from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass

import pandas as pd
from sqlalchemy.exc import DBAPIError

from .config import Settings
from .db import create_engines, drop_table, ensure_schema, fq_sql, truncate_table
from .tables import TableSpec

logger = logging.getLogger(__name__)


# -----------------------
# Logging-konfiguration
# -----------------------
def configure_logging(level: str = "INFO", logfile: str | None = None):
    """
    Simpel logging til konsol + (valgfrit) fil.
    """
    import sys
    from pathlib import Path

    handlers = [logging.StreamHandler(sys.stdout)]
    if logfile:
        path = Path(logfile)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
    )
    # Skru ned for SQLAlchemy chattiness
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# -----------------------
# ETL-telemetri
# -----------------------
@dataclass
class TableResult:
    table: TableSpec
    rows: int
    seconds: float
    ok: bool
    error: str | None = None

    @property
    def rps(self) -> float:
        return 0.0 if self.seconds <= 0 else self.rows / self.seconds


@dataclass
class RunSummary:
    total_tables: int
    ok_tables: int
    failed_tables: int
    total_rows: int
    seconds: float


def _is_retryable_db_error(exc: Exception) -> bool:
    """
    Heuristik: vurder om en DB-fejl er midlertidig, og det giver mening at prøve igen.
    Lige nu: SQL Server "SHUTDOWN is in progress" (6005) og lignende.
    """
    msg = str(exc.orig) if isinstance(exc, DBAPIError) and exc.orig is not None else str(exc)
    return "SHUTDOWN IS IN PROGRESS" in msg.upper()


# -----------------------
# ETL-klasse
# -----------------------
class KonsidoETL:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.synapse_engine, self.local_engine = create_engines(settings)

        # Sørg for at destinationskemaet findes (default dbo)
        dest_schema = getattr(self.settings, "LOCAL_DEST_SCHEMA_DEFAULT", "dbo") or "dbo"
        # dbo findes typisk – ignorer fejl
        with suppress(Exception):
            ensure_schema(self.local_engine, dest_schema)

    # --------- helpers ---------
    def _src_schema(self, table: TableSpec) -> str:
        """Kildeskema: tabellens eget override, ellers env-default."""
        return table.source_schema_effective(self.settings.AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT)

    def _dst_schema(self, table: TableSpec) -> str:
        """Destinationskema: tabellens eget override, ellers env-default."""
        return table.dest_schema_effective(self.settings.LOCAL_DEST_SCHEMA_DEFAULT or "dbo")

    # --------- EXTRACT ---------
    def extract_chunks(self, table: TableSpec) -> Iterable[pd.DataFrame]:
        """
        Hent data i chunks fra Synapse. Bruger SELECT * (du kan filtrere her om ønsket).
        """
        src_schema = self._src_schema(table)
        # Bracket-quote for SQL Server (fq_sql escaper ']')
        query = f"SELECT * FROM {fq_sql(src_schema, table.name)}"
        logger.info("Henter data fra Synapse: %s", query)
        with self.synapse_engine.connect() as conn:
            yield from pd.read_sql(query, conn, chunksize=self.settings.ETL_CHUNKSIZE)

    # --------- LOAD ---------
    def _prepare_destination(self, dest_schema: str, dest_table_name: str):
        """
        Håndterer overskrivnings-mode på lokal DB.
        - DROP_CREATE: dropper tabellen (hvis findes). Første chunk vil oprette den.
        - TRUNCATE: forsøger TRUNCATE; hvis tabellen ikke findes, ignoreres det stilfærdigt.
        """
        mode = self.settings.ETL_OVERWRITE_MODE.upper()
        # sørg for at schema eksisterer
        with suppress(Exception):
            ensure_schema(self.local_engine, dest_schema)

        if mode == "DROP_CREATE":
            # Brug schema-aware drop
            drop_table(self.local_engine, schema=dest_schema, name=dest_table_name)
        elif mode == "TRUNCATE":
            try:
                truncate_table(self.local_engine, schema=dest_schema, name=dest_table_name)
            except Exception:
                # typisk: tabellen findes ikke (første kørsel) – OK
                logger.debug(
                    "TRUNCATE ignoreret (muligvis ny tabel): %s.%s",
                    dest_schema,
                    dest_table_name,
                )
        else:
            logger.warning("Ukendt ETL_OVERWRITE_MODE=%s – bruger TRUNCATE som fallback", mode)
            with suppress(Exception):
                truncate_table(self.local_engine, schema=dest_schema, name=dest_table_name)

    def load_table(self, table: TableSpec) -> TableResult:
        """
        Loader én tabel fra Synapse til lokal DB.
        - Læser fra [<source_schema>].[<name>] i Synapse
        - Skriver til [<dest_schema>].[<name>] i lokal DB
        """
        # Disse kan evt. sættes i .env via Settings, ellers bruges defaults her
        max_retries = getattr(self.settings, "ETL_MAX_RETRIES", 2)
        base_delay = getattr(self.settings, "ETL_RETRY_BASE_DELAY_SECONDS", 300)
        max_delay = getattr(self.settings, "ETL_RETRY_MAX_DELAY_SECONDS", 900)

        dest_name = table.name
        dest_schema = self._dst_schema(table)
        mode = self.settings.ETL_OVERWRITE_MODE.upper()

        attempt = 0
        while True:
            start = time.perf_counter()
            total_rows = 0

            # For hvert forsøg forbereder vi destinationen igen for at undgå dubletter.
            self._prepare_destination(dest_schema, dest_name)

            try:
                first = True
                for chunk in self.extract_chunks(table):
                    total_rows += len(chunk)
                    logger.info(
                        "Chunk modtaget: %s rækker, %s kolonner", len(chunk), len(chunk.columns)
                    )

                    if first:
                        if_exists = "replace" if mode == "DROP_CREATE" else "append"
                        # Ved første chunk opretter vi tabellen hvis den ikke findes
                        chunk.to_sql(
                            dest_name,
                            self.local_engine,
                            schema=dest_schema,  # <— skriv til dbo (eller valgt skema)
                            if_exists=if_exists,
                            index=False,
                            method=None,  # fast_executemany sættes i db.py via event hook
                        )
                        first = False
                    else:
                        chunk.to_sql(
                            dest_name,
                            self.local_engine,
                            schema=dest_schema,
                            if_exists="append",
                            index=False,
                            method=None,
                        )

                seconds = time.perf_counter() - start
                logger.info(
                    "✔ %s.%s → %s.%s: %s rækker (%.1f r/s, %.2fs)",
                    self._src_schema(table),
                    table.name,
                    dest_schema,
                    dest_name,
                    total_rows,
                    0 if seconds <= 0 else total_rows / seconds,
                    seconds,
                )
                return TableResult(table=table, rows=total_rows, seconds=seconds, ok=True)

            except Exception as e:
                seconds = time.perf_counter() - start
                logger.exception(
                    "✘ Fejl ved load af %s.%s → %s.%s (forsøg %s): %s",
                    self._src_schema(table),
                    table.name,
                    dest_schema,
                    dest_name,
                    attempt + 1,
                    e,
                )

                # Skal vi prøve igen?
                if attempt < max_retries and _is_retryable_db_error(e):
                    attempt += 1
                    # simpel backoff: base, 2*base, ... men clamp til max_delay
                    delay = min(base_delay * attempt, max_delay)
                    logger.warning(
                        "Midlertidig DB-fejl (forsøg %s/%s). Prøver igen om %s sekunder.",
                        attempt,
                        max_retries,
                        delay,
                    )
                    # Luk eksisterende forbindelser – især vigtigt ved server-restart
                    with suppress(Exception):
                        self.local_engine.dispose()
                    time.sleep(delay)
                    continue

                # Ingen flere retries, eller ikke en midlertidig fejl → giv op
                return TableResult(
                    table=table, rows=total_rows, seconds=seconds, ok=False, error=str(e)
                )

    # --------- ORCHESTRATION ---------
    def run(self, tables: list[TableSpec]) -> RunSummary:
        """
        Kør de angivne tabeller, returnér en kort opsummering.

        Bemærk: cli.py::run har sin egen løkke med output til terminalen. Denne
        metode er tænkt til brug som bibliotek.
        """
        selected = list(tables)
        t0 = time.perf_counter()

        results: list[TableResult] = []
        for t in selected:
            src_schema = self._src_schema(t)
            dst_schema = self._dst_schema(t)
            logger.info(
                "→ Starter load: %s.%s (src_schema=%s, dst_schema=%s)",
                src_schema,
                t.name,
                src_schema,
                dst_schema,
            )
            res = self.load_table(t)
            results.append(res)

        total = len(results)
        ok = sum(1 for r in results if r.ok)
        failed = total - ok
        rows = sum(r.rows for r in results)
        seconds = time.perf_counter() - t0

        # Lille tabel/oversigt i loggen
        logger.info("== ETL opsummering ==")

        def _disp_name(tblspec: TableSpec) -> str:
            return f"{self._src_schema(tblspec)}.{tblspec.name}"

        width = max(12, max(len(_disp_name(r.table)) for r in results) if results else 12)
        for r in results:
            name = _disp_name(r.table).ljust(width)
            status = "OK" if r.ok else "FAIL"
            speed = f"{r.rps:.0f} r/s" if r.seconds > 0 else "-"
            logger.info(
                "%s | %-4s | %8d rows | %6.2fs | %s", name, status, r.rows, r.seconds, speed
            )
        logger.info(
            "TOTAL: %s tabeller (%s OK, %s fejl), %s rækker, %.2fs (≈ %s r/s)",
            total,
            ok,
            failed,
            rows,
            seconds,
            0 if seconds <= 0 else int(rows / seconds),
        )

        return RunSummary(
            total_tables=total,
            ok_tables=ok,
            failed_tables=failed,
            total_rows=rows,
            seconds=seconds,
        )
