"""Runs one export job in its own process (started by app/exports.py).

    python -m app.export_worker <job_id>
"""
import logging
import sys

from app import database as db
from app import exports


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(argv) != 1 or not exports.JOB_ID_RE.match(argv[0]):
        print("usage: python -m app.export_worker <job_id>", file=sys.stderr)
        return 2
    db.open_pool(wait=True)
    try:
        exports.run_job(argv[0])
    finally:
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
