"""Where the governance store lives — one definition for db/seed.py and alembic.

A copy in each meant a change could leave migrations and seeding on different
databases.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from dotenv import load_dotenv

DB_DIR = Path(__file__).resolve().parent

# Host runs read .env; already-set variables win, so compose stays authoritative.
load_dotenv(DB_DIR.parent / ".env", override=False)

ENV_VAR = "FPA_GOVERNANCE_DB_URL"


def database_url() -> str:
    """FPA_GOVERNANCE_DB_URL, else the PG* variables, else alembic.ini.

    Compose sets PG* for psql and pg_isready anyway; deriving the URL from them
    avoids a second copy of the same facts.
    """
    if os.getenv(ENV_VAR):
        return os.environ[ENV_VAR]
    if os.getenv("PGHOST"):
        user = os.getenv("PGUSER", "postgres")
        password = os.getenv("PGPASSWORD", "")
        host = os.environ["PGHOST"]
        port = os.getenv("PGPORT", "5432")
        name = os.getenv("PGDATABASE", "fpa")
        return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(DB_DIR / "alembic.ini")
    return parser["alembic"]["sqlalchemy.url"]
