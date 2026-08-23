#!/usr/bin/env python3
"""Delete finished jobs (done/error) from jobs.db."""

import sqlite3
from pathlib import Path

BASE = Path.home() / "translate_service"
DB_PATH = BASE / "jobs.db"


def purge_finished_jobs() -> int:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.execute("SELECT job_id, status FROM jobs WHERE status IN ('done', 'error') ORDER BY created_at ASC")
        rows = cursor.fetchall()
        if not rows:
            print("No done/error jobs found in database.")
            return 0

        print(f"Found {len(rows)} finished job(s):")
        for job_id, status in rows:
            print(f"  - {job_id} ({status})")

        conn.executemany(
            "DELETE FROM jobs WHERE job_id = ?",
            [(job_id,) for job_id, _status in rows],
        )
        conn.commit()
        print(f"Deleted {len(rows)} finished job(s) from database.")
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    purge_finished_jobs()
