"""Daily housekeeping (scripts/maintenance.sh, cron 03:30 WIB).

- Activity log entries older than ACTIVITY_RETENTION_DAYS (default 90) are deleted.
- Google Sheets exports older than GSHEET_RETENTION_DAYS (default 30) are moved to the
  Drive trash of the account that owns them (Drive empties its trash after 30 days, so a
  sheet can still be restored there). 0 disables either clean-up.

    python -m app.maintenance
"""
import logging
import sys

from app import database as db
from app import gsheets
from app.activity import T as ACTIVITY_TABLE
from app.config import get_settings

logger = logging.getLogger("maintenance")


def purge_activity() -> int:
    days = get_settings().activity_retention_days
    if days <= 0:
        return 0
    with db.transaction(timeout_ms=600_000) as conn:
        return conn.execute(f"DELETE FROM {ACTIVITY_TABLE} WHERE created_at < now() - %s::interval",
                            (f"{days} days",)).rowcount


def purge_sheets() -> int:
    days = get_settings().gsheet_retention_days
    if days <= 0 or not gsheets.enabled():
        return 0
    return gsheets.cleanup(days)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    failed = False
    for name, job in (("activity log", purge_activity), ("google sheets", purge_sheets)):
        try:
            logger.info("%s: %s removed", name, job())
        except Exception:  # noqa: BLE001 - one failing clean-up must not stop the other
            logger.exception("%s clean-up failed", name)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
