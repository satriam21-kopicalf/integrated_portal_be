"""Apply SQL migrations in app/migrations/ to the integration_portal schema.

    python -m app.migrate          # apply pending migrations
    python -m app.migrate --list   # show applied / pending

Each file runs once, inside a transaction, and is recorded in
integration_portal.schema_migrations. Run by scripts/auto-deploy.sh after every
deploy, so adding a numbered .sql file is all a schema change needs.
"""
import sys
from pathlib import Path

from app import database as db

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS integration_portal;
CREATE TABLE IF NOT EXISTS integration_portal.schema_migrations (
    name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def pending() -> tuple[list[str], list[str]]:
    with db.transaction() as conn:
        conn.execute(BOOTSTRAP)
        applied = {r["name"] for r in conn.execute("SELECT name FROM integration_portal.schema_migrations").fetchall()}
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f in applied], [f for f in files if f not in applied]


def migrate() -> list[str]:
    _, todo = pending()
    for name in todo:
        sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        with db.transaction() as conn:
            conn.execute(sql)
            conn.execute("INSERT INTO integration_portal.schema_migrations (name) VALUES (%s)", (name,))
        print(f"applied {name}")
    return todo


if __name__ == "__main__":
    db.open_pool(wait=True)
    try:
        if "--list" in sys.argv:
            done, todo = pending()
            for n in done:
                print(f"applied  {n}")
            for n in todo:
                print(f"pending  {n}")
        else:
            applied = migrate()
            print(f"{len(applied)} migration(s) applied" if applied else "schema up to date")
    finally:
        db.close_pool()
