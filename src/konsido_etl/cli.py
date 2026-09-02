from __future__ import annotations

import os
import sys

import typer
from pydantic import ValidationError

from .config import Settings
from .db import create_engines
from .etl import KonsidoETL, configure_logging
from .tables import TableConfigError, TableSpec, load_tables

app = typer.Typer(add_completion=False, help="Konsido ETL – CLI")

# intern status til exit code
_RUN_ERRORS: list[int] = []

# Gyldige værdier for --mode / ETL_OVERWRITE_MODE
_VALID_OVERWRITE_MODES = ("TRUNCATE", "DROP_CREATE")


def _redact(value: str | None) -> str:
    """
    Skjul en hemmelighed fuldstændigt.

    Vi viser hverken længde eller dele af værdien: `show-config` ender ofte i
    terminal-historik, screenshots og supportsager.
    """
    if not value:
        return "(ikke sat)"
    return "***"


def _settings_or_exit() -> Settings:
    """
    Indlæs Settings og oversæt en ValidationError til feltnavne alene.

    Pydantics egen fejltekst indeholder `input_value=` med hele
    konfigurationsdictet. Den forkortes på midten, så halen af
    AZURE_SYNAPSE_ADGANGSKODE kan stå i klartekst — og stderr fra de planlagte
    .bat-scripts ender i logs\\. Derfor rører vi aldrig exc' råtekst.
    """
    try:
        return Settings()
    except ValidationError as exc:
        typer.echo("FEJL: konfigurationen kunne ikke indlæses.", err=True)
        for err in exc.errors():
            field = ".".join(str(part) for part in err["loc"]) or "(ukendt felt)"
            typer.echo(f"  - {field}: {err['msg']}", err=True)
        typer.echo("Sammenlign .env med .env.example.", err=True)
        raise typer.Exit(code=2) from None


def _load_tables_or_exit(settings: Settings) -> list[TableSpec]:
    """Indlæs tabelfilen og giv en læsbar fejl i stedet for en traceback."""
    try:
        return load_tables(settings.ETL_TABLES_FILE)
    except TableConfigError as exc:
        typer.echo(f"FEJL: {exc}", err=True)
        raise typer.Exit(code=2) from None


def _resolve_tables(all_tables: list[TableSpec], names: list[str] | None) -> list[TableSpec]:
    if not names:
        return all_tables
    name_set = {n.lower() for n in names}
    resolved = [t for t in all_tables if t.name.lower() in name_set]
    missing = name_set - {t.name.lower() for t in resolved}
    if missing:
        missing_str = ", ".join(sorted(missing))
        valid = ", ".join(sorted(t.name for t in all_tables))
        raise typer.BadParameter(f"Ukendte tabeller: {missing_str}. Gyldige: {valid}")
    return resolved


# -------- schema helpers (holdes i CLI for ikke at importere fra etl) --------
def _src_schema(settings: Settings, table: TableSpec) -> str:
    return table.source_schema_effective(settings.AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT)


def _dst_schema(settings: Settings, table: TableSpec) -> str:
    return table.dest_schema_effective(settings.LOCAL_DEST_SCHEMA_DEFAULT or "dbo")


@app.command()
def version():
    """Vis version og miljø."""
    from . import __version__

    typer.echo(f"konsido-etl {__version__}")
    typer.echo(f"Python {sys.version.split()[0]}")


@app.command()
def show_config():
    """Vis effektiv konfiguration (adgangskoder skjules)."""
    s = _settings_or_exit()
    data = {
        "KONSIDO_AZURE_SYNAPSE": s.KONSIDO_AZURE_SYNAPSE,
        "AZURE_SYNAPSE_BRUGERNAVN": s.AZURE_SYNAPSE_BRUGERNAVN,
        "AZURE_SYNAPSE_ADGANGSKODE": _redact(s.AZURE_SYNAPSE_ADGANGSKODE),
        "AZURE_SYNAPSE_DATABASE": s.AZURE_SYNAPSE_DATABASE,
        # schema defaults
        "AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT": s.AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT,
        "LOCAL_DEST_SCHEMA_DEFAULT": s.LOCAL_DEST_SCHEMA_DEFAULT,
        "ETL_TABLES_FILE": s.ETL_TABLES_FILE,
        # local db
        "LOKAL_DB_URL": (s.LOKAL_DB_URL or "(constructed)"),
        "LOKAL_DB": s.LOKAL_DB,
        "LOKAL_DB_PORT": s.LOKAL_DB_PORT,
        "LOKAL_DB_BRUGERNAVN": s.LOKAL_DB_BRUGERNAVN,
        "LOKAL_DB_ADGANGSKODE": _redact(s.LOKAL_DB_ADGANGSKODE),
        "LOKAL_DB_NAVN": s.LOKAL_DB_NAVN,
        # etl + logging
        "ETL_CHUNKSIZE": s.ETL_CHUNKSIZE,
        "ETL_OVERWRITE_MODE": s.ETL_OVERWRITE_MODE,
        "LOG_LEVEL": s.LOG_LEVEL,
        "LOG_FILE": os.getenv("LOG_FILE", None),
    }
    for k, v in data.items():
        typer.echo(f"{k} = {v}")


@app.command()
def list_tables():
    """List alle tabeller og deres effektive kilde- og destinationsskemaer."""
    s = _settings_or_exit()
    tables = _load_tables_or_exit(s)
    for t in tables:
        typer.echo(f"- {_src_schema(s, t)}.{t.name}  →  {_dst_schema(s, t)}.{t.name}")
    typer.echo(f"({len(tables)} tabeller fra {s.ETL_TABLES_FILE})")


@app.command()
def test_conn():
    """Test forbindelser til Synapse og lokal DB."""
    s = _settings_or_exit()
    syn, loc = create_engines(s)
    try:
        with syn.connect() as c:
            c.exec_driver_sql("SELECT 1")
        typer.echo("OK: Forbindelse til Azure Synapse")
    except Exception as e:  # pragma: no cover
        typer.echo(f"FEJL: Synapse – {e}")
        # 'from None': den underliggende driverfejl er allerede vist, og dens
        # traceback kan indeholde forbindelsesdetaljer.
        raise typer.Exit(code=1) from None

    try:
        with loc.connect() as c:
            c.exec_driver_sql("SELECT 1")
        typer.echo("OK: Forbindelse til lokal DB")
    except Exception as e:  # pragma: no cover
        typer.echo(f"FEJL: Lokal DB – {e}")
        # 'from None': den underliggende driverfejl er allerede vist, og dens
        # traceback kan indeholde forbindelsesdetaljer.
        raise typer.Exit(code=1) from None


@app.command()
def run(
    tables: list[str] | None = typer.Option(
        None,
        "--table",
        "-t",
        help="Kør kun udvalgte tabeller (kan angives flere gange).",
        metavar="NAME",
    ),
    overwrite_mode: str | None = typer.Option(
        None,
        "--mode",
        help="Tilsidesæt ETL_OVERWRITE_MODE for denne kørsel (TRUNCATE eller DROP_CREATE)",
    ),
    log_file: str | None = typer.Option(
        None,
        "--log-file",
        help=(
            "Gem log i fil (tilføjes også til konsol). "
            "Kan også sættes via miljøvariablen LOG_FILE."
        ),
    ),
):
    """Kør hele ETL’en (eller et subset af tabeller)."""
    s = _settings_or_exit()
    if overwrite_mode:
        # Valider før override: en ukendt værdi må ikke falde igennem til
        # TRUNCATE-fallbacken i _prepare_destination().
        mode = overwrite_mode.strip().upper()
        if mode not in _VALID_OVERWRITE_MODES:
            raise typer.BadParameter(
                f"Ugyldig værdi '{overwrite_mode}'. "
                f"Gyldige: {', '.join(_VALID_OVERWRITE_MODES)}",
                param_hint="--mode",
            )
        # midlertidigt override af mode for netop denne kørsel
        s.ETL_OVERWRITE_MODE = mode  # type: ignore[attr-defined]

    # Logging til konsol + evt. fil
    log_file = log_file or os.getenv("LOG_FILE")
    try:
        configure_logging(s.LOG_LEVEL, log_file)  # type: ignore[misc]
    except TypeError:
        configure_logging(s.LOG_LEVEL)  # type: ignore[call-arg]

    selected = _resolve_tables(_load_tables_or_exit(s), tables)
    etl = KonsidoETL(s)

    for t in selected:
        src = _src_schema(s, t)
        dst = _dst_schema(s, t)
        typer.echo(f"→ Loader {src}.{t.name}  →  {dst}.{t.name} …")
        try:
            result = etl.load_table(t)
        except Exception as e:  # pragma: no cover
            # Hvis noget alligevel slipper igennem retries og TableResult
            typer.echo(f"✗ Ufanget fejl for {src}.{t.name} → {dst}.{t.name}: {e}")
            _RUN_ERRORS.append(1)
            continue

        if result.ok:
            typer.echo(
                f"✓ Færdig: {src}.{t.name}  →  {dst}.{t.name} "
                f"({result.rows} rækker, {result.seconds:.2f}s)"
            )
        else:
            typer.echo(f"✗ Fejl for {src}.{t.name} → {dst}.{t.name}: {result.error}")
            _RUN_ERRORS.append(1)

    raise typer.Exit(code=1 if _RUN_ERRORS else 0)


if __name__ == "__main__":
    app()
