#!/usr/bin/env python3
"""Migrate existing queue/ jobs to SQLite database.

This script is a one-time utility to register jobs from queue/ into the
newly-added database. After this, the worker.py will automatically register
new queue files into the database.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path.home() / "translate_service"
QUEUE = BASE / "queue"
DB_PATH = BASE / "jobs.db"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def init_db() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                input_video TEXT,
                output_video TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def migrate() -> int:
    init_db()
    
    conn = sqlite3.connect(str(DB_PATH))
    migrated = 0
    try:
        queue_files = sorted(QUEUE.glob("*.json"))
        for qfile in queue_files:
            try:
                job_data = json.loads(qfile.read_text(encoding="utf-8"))
                job_id = job_data.get("job_id") or qfile.stem
                
                # Check if job already exists
                cursor = conn.execute("SELECT job_id FROM jobs WHERE job_id = ?", (job_id,))
                if cursor.fetchone():
                    print(f"  {job_id}: already in DB (skipped)")
                    continue
                
                # Insert new job
                conn.execute(
                    """
                    INSERT INTO jobs
                    (job_id, status, input_video, output_video, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        "queued",
                        job_data.get("input_video"),
                        job_data.get("output_video"),
                        now_iso(),
                    ),
                )
                conn.commit()
                print(f"  {job_id}: registered as queued")
                migrated += 1
            except Exception as e:
                print(f"  {qfile.name}: ERROR: {e}")
    finally:
        conn.close()
    
    return migrated


if __name__ == "__main__":
    print(f"Migrating queue/ jobs to {DB_PATH}...")
    count = migrate()
    print(f"Migration complete. {count} new jobs registered.")
