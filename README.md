# Konsido ETL (Synapse → lokal SQL Server)

Daglig overførsel af tabeller fra en serverless SQL pool i Azure Synapse til en lokal
SQL Server. Data læses i chunks med `pandas.read_sql` og skrives med `pandas.to_sql`.

Hver kørsel **overskriver** indholdet af destinationstabellerne. Der er ingen
inkrementel indlæsning og ingen CDC — det er et bevidst valg, ikke en mangel.

## Krav

- Python 3.11 eller 3.12
- [`uv`](https://docs.astral.sh/uv/)
- **ODBC Driver 17 for SQL Server** (kan ændres via `SYNAPSE_ODBC_DRIVER` /
  `LOCAL_ODBC_DRIVER`)
- Netværksadgang til Synapse-endpointet på TCP 1433
- En lokal SQL Server, som brugeren har rettigheder til at oprette, tømme og droppe
  tabeller i

Destinationen skal være SQL Server. Der er ingen Postgres- eller SQLite-fallback:
mangler den lokale konfiguration, fejler `local_sqlalchemy_url()` med en `ValueError`.

## Installation

```bash
uv sync
cp .env.example .env
# udfyld .env
```

Alle kommandoer undtagen `--help` konstruerer `Settings()`, som kræver at
Synapse-variablerne er sat. Uden en udfyldt `.env` kører ingenting.

## Kommandoer

```bash
uv run konsido-etl version       # version og Python-version
uv run konsido-etl show-config   # effektiv konfiguration, hemmeligheder skjult
uv run konsido-etl list-tables   # kilde.tabel -> destination.tabel
uv run konsido-etl test-conn     # åbner rigtige forbindelser til begge databaser
uv run konsido-etl run           # hele ETL'en
```

Udvalgte tabeller og midlertidigt override af overskrivnings-mode:

```bash
uv run konsido-etl run -t fact_spend -t dim_supplier --mode DROP_CREATE
uv run konsido-etl run --log-file logs/konsido-etl.log
```

`run` afslutter med exit code 1, hvis mindst én tabel fejlede, ellers 0 — brugbart for
en planlagt opgave.

## Overskrivnings-modes

| Mode          | Adfærd                                                                 |
| ------------- | ---------------------------------------------------------------------- |
| `TRUNCATE`    | `TRUNCATE TABLE`, med `DELETE FROM` som fallback (fx ved fremmednøgler) |
| `DROP_CREATE` | `DROP TABLE`; første chunk opretter tabellen igen                       |

I begge tilfælde udledes destinationens kolonnetyper af pandas/SQLAlchemy ud fra det
**første** chunk. Der findes ingen eksplicit DDL i projektet. Har du brug for præcise
typer, indeks eller primærnøgler, skal tabellerne oprettes på forhånd, og
`if_exists="append"` bruges hele vejen.

## Fejlhåndtering

Fejler en tabel, prøves den igen, hvis fejlen vurderes midlertidig — i praksis
`SHUTDOWN IS IN PROGRESS` fra en SQL Server, der genstarter. Der forsøges op til
`ETL_MAX_RETRIES` gange med lineær backoff, begrænset af
`ETL_RETRY_MAX_DELAY_SECONDS`. Hvert forsøg forbereder destinationen forfra, så et
delvist load ikke giver dubletter.

Andre fejl afbryder den enkelte tabel, og kørslen fortsætter til næste.

## Planlægning

Driften kører på Windows via Task Scheduler. De to wrapper-scripts ligger i repoet og
bruger kun relative stier, så de virker fra et vilkårligt checkout:

| Script                       | Mode          | Tabeller           | Planlagt |
| ---------------------------- | ------------- | ------------------ | -------- |
| `run_daily.bat`              | `TRUNCATE`    | alle               | ja       |
| `run_schema_refresh.bat`     | `DROP_CREATE` | et udvalgt subset  | ja       |
| `run_test_single_table.bat`  | `TRUNCATE`    | én, valgfri        | nej      |

`run_test_single_table.bat` er til manuel brug og tager tabellen som argument —
uden argument bruges `dim_date`, så et smoketjek er billigt:

```bat
.\run_test_single_table.bat
.\run_test_single_table.bat fact_spend
```

`run_daily.bat` beholder tabellernes struktur og udskifter kun indholdet.
`run_schema_refresh.bat` dropper og genopretter de valgte tabeller, så ændrede
kolonner i kilden slår igennem — destinationens typer udledes af det første chunk.

Begge scripts sætter `PYTHONUTF8=1` og `chcp 65001`, kalder
`.\.venv\Scripts\python.exe -m konsido_etl.cli`, og skriver både en kørselslog og en
planlægningslog til `logs\`. De videregiver ETL'ens exit code, så Task Scheduler kan
se en fejlet kørsel:

| Exit code | Betydning                                                              |
| --------- | ---------------------------------------------------------------------- |
| `0`       | alle tabeller gik igennem                                              |
| `1`       | mindst én tabel fejlede                                                |
| `2`       | konfigurations- eller parameterfejl — intet blev forsøgt indlæst       |

Exit code `2` dækker manglende `.env`-variabler, en manglende eller ugyldig
tabelfil, en ukendt `--mode` og et ukendt `--table`. Alle fire tjekkes før der
åbnes forbindelse, så en tastefejl i et script koster ingenting.

På Linux svarer det til en cron-linje:

```cron
15 2 * * * cd /sti/til/konsido-etl && uv run konsido-etl run
```

Bemærk indholdet af `logs\` — se afsnittet om data nedenfor.

## Tilpasning af tabeller

Tabellisten er konfiguration, ikke kode. Den ligger i en TOML-fil — som standard
`tables.toml` i projektroden, ændres med `ETL_TABLES_FILE` i `.env`:

```toml
[[table]]
name = "fact_spend"

[[table]]
name = "dim_date"
source_schema = "faelles"   # valgfri, overskriver env-default
dest_schema = "staging"     # valgfri, overskriver env-default
```

Kopiér `tables.example.toml` til `tables.toml` og udfyld med jeres egne tabeller.
Uden `source_schema`/`dest_schema` bruges `AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT` og
`LOCAL_DEST_SCHEMA_DEFAULT`.

Navne valideres mod `^[A-Za-z_][A-Za-z0-9_-]*$`, før de sættes ind i SQL. Filen er
en tillidsgrænse: den skal kun kunne redigeres af dem, der også må ændre koden.
`konsido-etl list-tables` viser den indlæste liste og afslutter med exit code 2, hvis
filen mangler eller er ugyldig.

## Om data

Kilden er kommunale forbrugs- og fakturadata: rigtige leverandører, fakturalinjer og
posteringer. Behandl rækker, logfiler og fejlbeskeder som person- og økonomioplysninger.
`logs/`, `.data/` og lokale `*.db`-filer er derfor i `.gitignore`.

## Status

Der er ingen tests i projektet. Ændringer verificeres ved en manuel kørsel mod en
testdatabase.

## Licens og support

Licenseret under Apache License 2.0 — se [LICENSE](LICENSE) og [NOTICE](NOTICE).

Koden deles **som den er**, uden nogen form for support, vedligeholdelses- eller
serviceforpligtelse. Den er skrevet til én kommunes opsætning og bør gennemgås, før
den bruges et andet sted. Spørgsmål og forbedringer er velkomne som issues eller
pull requests, men der er ingen garanti for svartid.
