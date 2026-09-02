from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Konservativt mønster for SQL Server-identifiers, vi selv vil sætte i klammer.
# Bogstaver, cifre, understreg og bindestreg — nok til fx "min-kommune".
# Formålet er ikke fuld validering, men at holde ']' og andre skæve tegn ude af
# de f-strings, der bygger SELECT/TRUNCATE/DROP.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class TableConfigError(RuntimeError):
    """Tabelfilen mangler, kan ikke parses, eller indeholder ugyldige navne."""


@dataclass(frozen=True)
class TableSpec:
    name: str
    # Valgfri overrides pr. tabel. Er de None, bruges env-defaults.
    source_schema: str | None = None
    dest_schema: str | None = None

    def source_schema_effective(self, default_src: str | None) -> str:
        """Prioritet: tabellens eget source_schema > env-default > 'dbo'."""
        return self.source_schema or default_src or "dbo"

    def dest_schema_effective(self, default_dst: str) -> str:
        """Prioritet: tabellens eget dest_schema > env-default."""
        return self.dest_schema or default_dst


def _validate_ident(value: str, *, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TableConfigError(f"[[table]] nr. {index}: '{field}' skal være en ikke-tom tekst.")
    value = value.strip()
    if not _IDENT_RE.match(value):
        raise TableConfigError(
            f"[[table]] nr. {index}: '{field}' = {value!r} er ikke et gyldigt navn. "
            "Tilladt: bogstaver, cifre, understreg og bindestreg, startende med "
            "bogstav eller understreg."
        )
    return value


def load_tables(path: str | Path) -> list[TableSpec]:
    """
    Indlæs tabellisten fra en TOML-fil.

    Forventet format:

        [[table]]
        name = "fact_spend"

        [[table]]
        name = "dim_supplier"
        source_schema = "andet-skema"   # valgfri
        dest_schema = "staging"         # valgfri

    Rejser TableConfigError ved manglende fil, ugyldig TOML, dubletter eller
    navne, der ikke ser ud som SQL Server-identifiers.
    """
    p = Path(path)
    if not p.is_file():
        raise TableConfigError(
            f"Tabelfilen findes ikke: {p}. Sæt ETL_TABLES_FILE i .env, eller kopiér "
            "tables.example.toml til tables.toml."
        )

    try:
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise TableConfigError(f"{p} kunne ikke læses som TOML: {exc}") from exc
    except OSError as exc:
        raise TableConfigError(f"{p} kunne ikke åbnes: {exc}") from exc

    entries = raw.get("table")
    if not isinstance(entries, list) or not entries:
        raise TableConfigError(f"{p} indeholder ingen [[table]]-blokke.")

    specs: list[TableSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise TableConfigError(f"[[table]] nr. {i} i {p} er ikke en tabel-blok.")

        unknown = set(entry) - {"name", "source_schema", "dest_schema"}
        if unknown:
            raise TableConfigError(
                f"[[table]] nr. {i} i {p}: ukendte nøgler: {', '.join(sorted(unknown))}"
            )

        if "name" not in entry:
            raise TableConfigError(f"[[table]] nr. {i} i {p} mangler 'name'.")

        name = _validate_ident(entry["name"], field="name", index=i)
        if name.lower() in seen:
            raise TableConfigError(f"{p}: tabellen {name!r} står mere end én gang.")
        seen.add(name.lower())

        source_schema = entry.get("source_schema")
        if source_schema is not None:
            source_schema = _validate_ident(source_schema, field="source_schema", index=i)

        dest_schema = entry.get("dest_schema")
        if dest_schema is not None:
            dest_schema = _validate_ident(dest_schema, field="dest_schema", index=i)

        specs.append(TableSpec(name=name, source_schema=source_schema, dest_schema=dest_schema))

    return specs
