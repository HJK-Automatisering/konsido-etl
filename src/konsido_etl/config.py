from __future__ import annotations

from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

OverwriteMode = Literal["TRUNCATE", "DROP_CREATE"]


class Settings(BaseSettings):
    """
    Central konfiguration for Konsido ETL.

    - Læses automatisk fra `.env` (via pydantic-settings).
    - Giver helper-metoder til at bygge SQLAlchemy-URLs for både Synapse og lokal DB.
    """

    # Pydantic Settings konfiguration
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- Azure Synapse (SQL) ----------
    # Eksempel endpoint: myws-ondemand.sql.azuresynapse.net (serverless)
    # eller <workspace>.sql.azuresynapse.net
    KONSIDO_AZURE_SYNAPSE: str
    AZURE_SYNAPSE_BRUGERNAVN: str
    AZURE_SYNAPSE_ADGANGSKODE: str
    # Ingen defaults her: database og skema er specifikke for den enkelte
    # installation og hører i .env, ikke i koden. Begge er bevidst påkrævede —
    # et stille fallback til fx "dbo" ville give et planlagt job, der kører
    # videre og fejler på hver enkelt tabel i stedet for at stoppe med det samme.
    AZURE_SYNAPSE_DATABASE: str
    AZURE_SYNAPSE_SOURCE_SCHEMA_DEFAULT: str

    # ODBC-driver og sikkerhedsparametre (justerbar via env)
    SYNAPSE_ODBC_DRIVER: str = "ODBC Driver 17 for SQL Server"
    SYNAPSE_ENCRYPT: Literal["yes", "no"] = "yes"
    SYNAPSE_TRUST_SERVER_CERTIFICATE: Literal["yes", "no"] = "no"
    SYNAPSE_TRUSTED_CONNECTION: Literal["yes", "no"] = "no"

    # ---------- Lokal DB (SQL Server via ODBC 17) ----------
    # Du kan sætte en fuld SQLAlchemy-URL direkte:
    #   LOKAL_DB_URL="mssql+pyodbc://user:pwd@host,1433/db?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=no"
    LOKAL_DB_URL: str | None = None

    # ...eller udfylde felterne herunder, så bygges URL'en automatisk
    LOKAL_DB: str | None = None  # host (fx "localhost")
    LOKAL_DB_PORT: int | None = None  # fx 1433
    LOKAL_DB_BRUGERNAVN: str | None = None
    LOKAL_DB_ADGANGSKODE: str | None = None
    LOKAL_DB_NAVN: str | None = None  # database navn
    LOCAL_DEST_SCHEMA_DEFAULT: str = "dbo"

    LOCAL_ODBC_DRIVER: str = "ODBC Driver 17 for SQL Server"
    LOCAL_ENCRYPT: Literal["yes", "no"] = "no"
    LOCAL_TRUST_SERVER_CERTIFICATE: Literal["yes", "no"] = "no"
    LOCAL_TRUSTED_CONNECTION: Literal["yes", "no"] = "no"

    # ---------- Tabeller ----------
    # TOML-fil med [[table]]-blokke. Se tables.example.toml.
    ETL_TABLES_FILE: str = "tables.toml"

    # ---------- ETL & Logging ----------
    ETL_CHUNKSIZE: int = 100_000
    ETL_OVERWRITE_MODE: OverwriteMode = "TRUNCATE"
    LOG_LEVEL: str = "INFO"
    # Valgfrit: sæt filsti i .env for at logge til fil ud over konsollen (bruges i cli.py)
    # LOG_FILE=logs/konsido-etl.log

    # Retry ved midlertidige DB-fejl (fx SHUTDOWN in progress)
    ETL_MAX_RETRIES: int = 2  # antal ekstra forsøg pr. tabel
    ETL_RETRY_BASE_DELAY_SECONDS: int = 300  # 5 min som standard
    ETL_RETRY_MAX_DELAY_SECONDS: int = 900  # max 15 min mellem forsøg

    # ---------- Helpers ----------
    @staticmethod
    def _encode(s: str) -> str:
        """URL-encode (særligt adgangskoder med specialtegn)."""
        return quote_plus(s)

    @staticmethod
    def _driver_q(driver: str) -> str:
        """Erstat mellemrum med +, så ODBC-driver navnet fungerer i query-string."""
        return driver.replace(" ", "+")

    def synapse_sqlalchemy_url(self) -> str:
        """
        Bygger en mssql+pyodbc SQLAlchemy-URL til Azure Synapse.

        Eksempel-output:
        mssql+pyodbc://user:pwd@myws-ondemand.sql.azuresynapse.net/interface_db
            ?driver=ODBC+Driver+17+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no&trusted_connection=no
        """
        user = self._encode(self.AZURE_SYNAPSE_BRUGERNAVN)
        pwd = self._encode(self.AZURE_SYNAPSE_ADGANGSKODE)
        host = self.KONSIDO_AZURE_SYNAPSE  # fx "myws-ondemand.sql.azuresynapse.net"
        db = self.AZURE_SYNAPSE_DATABASE

        driver_q = self._driver_q(self.SYNAPSE_ODBC_DRIVER)
        return (
            f"mssql+pyodbc://{user}:{pwd}@{host}/{db}"
            f"?driver={driver_q}"
            f"&Encrypt={self.SYNAPSE_ENCRYPT}"
            f"&TrustServerCertificate={self.SYNAPSE_TRUST_SERVER_CERTIFICATE}"
            f"&trusted_connection={self.SYNAPSE_TRUSTED_CONNECTION}"
        )

    def local_sqlalchemy_url(self) -> str:
        """
        Bygger en mssql+pyodbc SQLAlchemy-URL til din lokale SQL Server.
        Hvis LOKAL_DB_URL er sat, bruges den direkte.
        Ellers samles den ud fra felterne LOKAL_DB*, driver og sikkerhedsflag.

        Eksempel-output:
        mssql+pyodbc://sa:Pwd123!@localhost,1433/konsido
            ?driver=ODBC+Driver+17+for+SQL+Server&trusted_connection=no&Encrypt=no&TrustServerCertificate=no
        """
        if self.LOKAL_DB_URL:
            return self.LOKAL_DB_URL

        required = [
            self.LOKAL_DB,
            self.LOKAL_DB_PORT,
            self.LOKAL_DB_BRUGERNAVN,
            self.LOKAL_DB_ADGANGSKODE,
            self.LOKAL_DB_NAVN,
        ]
        if all(required):
            user = self._encode(self.LOKAL_DB_BRUGERNAVN or "")
            pwd = self._encode(self.LOKAL_DB_ADGANGSKODE or "")
            host = f"{self.LOKAL_DB},{self.LOKAL_DB_PORT}"
            db = self.LOKAL_DB_NAVN
            driver_q = self._driver_q(self.LOCAL_ODBC_DRIVER)
            return (
                f"mssql+pyodbc://{user}:{pwd}@{host}/{db}"
                f"?driver={driver_q}"
                f"&trusted_connection={self.LOCAL_TRUSTED_CONNECTION}"
                f"&Encrypt={self.LOCAL_ENCRYPT}"
                f"&TrustServerCertificate={self.LOCAL_TRUST_SERVER_CERTIFICATE}"
            )

        # Eksplicit ingen implicit Postgres/SQLite fallback, men vi kan give en klar fejl.
        # Hvis du VIL have en SQLite fallback, kan du aflåse nedenfor og returnere en sqlite-URL.
        raise ValueError(
            "LOKAL_DB_URL mangler, og felterne LOKAL_DB, LOKAL_DB_PORT, "
            "LOKAL_DB_BRUGERNAVN, LOKAL_DB_ADGANGSKODE, LOKAL_DB_NAVN er ikke alle udfyldt."
        )
