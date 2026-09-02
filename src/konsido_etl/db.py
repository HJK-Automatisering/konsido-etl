from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings

logger = logging.getLogger(__name__)


# ---------- Engine oprettelse ----------


def _make_engine(url: str) -> Engine:
    """
    Opret en SQLAlchemy Engine.
    - pool_pre_ping=True for at undgå 'stale' forbindelser i planlagt drift.
    - Aktiver pyodbc fast_executemany på MSSQL for hurtige bulk-inserts.
    """
    engine = create_engine(url, pool_pre_ping=True, future=True)

    # Aktiver fast_executemany for mssql+pyodbc (betydelig hastighedsgevinst ved pandas.to_sql)
    if engine.url.get_backend_name().startswith("mssql"):
        try:

            @event.listens_for(engine, "before_cursor_execute")
            def _fast_executemany(conn, cursor, statement, parameters, context, executemany):
                if executemany and hasattr(cursor, "fast_executemany"):
                    cursor.fast_executemany = True  # pyodbc feature

        except Exception:  # pragma: no cover
            logger.debug("Kunne ikke aktivere fast_executemany; fortsætter uden.")
    return engine


def create_engines(settings: Settings) -> tuple[Engine, Engine]:
    """
    Returnerer (synapse_engine, local_engine)
    """
    synapse_engine = _make_engine(settings.synapse_sqlalchemy_url())
    local_engine = _make_engine(settings.local_sqlalchemy_url())
    return synapse_engine, local_engine


# ---------- Utility ----------


def _fq_name_arg(
    table_fullname: str | None, schema: str | None, name: str | None
) -> tuple[str, str]:
    """
    Normaliser input til (schema, name).
    - Hvis table_fullname er sat, forventes 'schema.name' eller bare 'name' (så antager vi dbo).
    - Hvis schema/name er angivet separat, bruges de.
    """
    if table_fullname:
        if "." in table_fullname:
            s, n = table_fullname.split(".", 1)
            return s.strip(), n.strip()
        return "dbo", table_fullname.strip()
    if not name:
        raise ValueError("Tabellens navn mangler.")
    return (schema or "dbo", name)


def _bracket_ident(s: str) -> str:
    """
    Bracket-quoting til SQL Server-identifiers.

    ']' escapes som ']]', så et navn ikke kan bryde ud af klammerne.
    """
    return "[" + s.replace("]", "]]") + "]"


def fq_sql(schema: str, name: str) -> str:
    """Fuldt kvalificeret, bracket-quotet navn: [schema].[tabel]."""
    return f"{_bracket_ident(schema)}.{_bracket_ident(name)}"


@contextmanager
def begin(engine: Engine):
    """
    Context manager der åbner en transaktion og committer automatisk.
    Bruges hvor vi vil sikre atomicitet.
    """
    with engine.begin() as conn:
        yield conn


# ---------- Schema / eksistens ----------


def ensure_schema(engine: Engine, schema: str = "dbo"):
    """
    Opret schema hvis det ikke findes (SQL Server).
    """
    if not schema or schema.lower() == "dbo":
        return  # dbo findes altid
    sql = text(
        "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = :schema) "
        "EXEC('CREATE SCHEMA ' + QUOTENAME(:schema));"
    )
    with begin(engine) as conn:
        conn.execute(sql, {"schema": schema})


def table_exists(engine: Engine, schema: str, name: str) -> bool:
    """
    Tjek om tabel findes i SQL Server.
    """
    sql = text(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :name"
    )
    with engine.connect() as conn:
        res = conn.execute(sql, {"schema": schema, "name": name}).first()
    return res is not None


# ---------- Vedligehold (TRUNCATE/DROP) ----------


def truncate_table(
    engine: Engine,
    table_fullname: str | None = None,
    *,
    schema: str | None = None,
    name: str | None = None,
):
    """
    TRUNCATE TABLE; fallback til DELETE hvis TRUNCATE ikke er muligt (fx pga. FK).
    - Bagudkompatibel: kan kaldes med table_fullname="dbo.fact_spend" eller bare name="fact_spend".
    """
    s, n = _fq_name_arg(table_fullname, schema, name)
    fq = fq_sql(s, n)

    if not table_exists(engine, s, n):
        logger.debug("truncate_table: %s findes ikke – ignorerer.", fq)
        return

    try:
        with begin(engine) as conn:
            conn.execute(text(f"TRUNCATE TABLE {fq}"))
            logger.info("TRUNCATE TABLE %s udført.", fq)
    except SQLAlchemyError as e:
        logger.warning("TRUNCATE mislykkedes for %s (%s). Forsøger DELETE.", fq, e)
        with begin(engine) as conn:
            conn.execute(text(f"DELETE FROM {fq}"))
            logger.info("DELETE FROM %s udført.", fq)


def drop_table(
    engine: Engine,
    table_fullname: str | None = None,
    *,
    schema: str | None = None,
    name: str | None = None,
):
    """
    DROP TABLE IF EXISTS [schema].[table]
    """
    s, n = _fq_name_arg(table_fullname, schema, name)
    fq = fq_sql(s, n)
    with begin(engine) as conn:
        # Navnet til OBJECT_ID er værdi-formet og går derfor gennem en bind-parameter;
        # kun det bracket-quotede identifier interpoleres.
        conn.execute(
            text(f"IF OBJECT_ID(:objname, 'U') IS NOT NULL DROP TABLE {fq};"),
            {"objname": f"{s}.{n}"},
        )
        logger.info("DROP TABLE %s (hvis eksisterede).", fq)


# ---------- Healthcheck ----------


def ping(engine: Engine) -> bool:
    """
    Simpel ping – returnerer True hvis SELECT 1 lykkes.
    """
    try:
        with engine.connect() as c:
            c.exec_driver_sql("SELECT 1")
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Ping fejlede: %s", e)
        return False
