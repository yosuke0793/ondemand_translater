#!/usr/bin/env python3
"""List all jobs from SQLite database."""

import sqlite3
import sys
from pathlib import Path

BASE = Path.home() / "translate_service"
DB_PATH = BASE / "jobs.db"


def list_jobs(status_filter: str = None) -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)
    
    conn = sqlite3.connect(str(DB_PATH))
    try:
        query = "SELECT job_id, status, created_at, started_at, completed_at, error_message FROM jobs"
        params: list = []
        
        if status_filter:
            query += " WHERE status = ?"
            params.append(status_filter)
        
        query += " ORDER BY created_at DESC"
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        if not rows:
            print(f"No jobs found" + (f" with status '{status_filter}'" if status_filter else ""))
            return
        
        # Print header
        print(f"\n{'Job ID':<40} {'Status':<10} {'Created':<25}")
        print("-" * 80)
        
        for job_id, status, created_at, started_at, completed_at, error_msg in rows:
            created_short = created_at[:19] if created_at else "N/A"
            print(f"{job_id:<40} {status:<10} {created_short:<25}")
            
            if status == "error" and error_msg:
                print(f"  └─ Error: {error_msg}")
    
    finally:
        conn.close()


if __name__ == "__main__":
    status = sys.argv[1] if len(sys.argv) > 1 else None
    list_jobs(status)
