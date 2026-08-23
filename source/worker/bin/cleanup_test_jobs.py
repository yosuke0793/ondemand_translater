#!/usr/bin/env python3
"""Remove test jobs from database and queue/processing/done/error directories."""

import sqlite3
import sys
from pathlib import Path

BASE = Path.home() / "translate_service"
DB_PATH = BASE / "jobs.db"
QUEUE = BASE / "queue"
PROCESSING = BASE / "processing"
DONE = BASE / "done"
ERROR = BASE / "error"


def cleanup_test_jobs(dry_run: bool = True) -> int:
    """Remove test-sequential-job-* entries from DB and filesystem."""
    
    removed_count = 0
    
    # Database cleanup
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.execute(
                "SELECT job_id FROM jobs WHERE job_id LIKE 'test-sequential-job-%'"
            )
            test_jobs = [row[0] for row in cursor.fetchall()]
            
            if test_jobs:
                print(f"Found {len(test_jobs)} test job(s) in database:")
                for job_id in test_jobs:
                    print(f"  - {job_id}")
                
                if not dry_run:
                    for job_id in test_jobs:
                        conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                    conn.commit()
                    print(f"✓ Deleted {len(test_jobs)} job(s) from database")
                    removed_count += len(test_jobs)
                else:
                    print(f"[DRY-RUN] Would delete {len(test_jobs)} job(s)")
        finally:
            conn.close()
    
    # Filesystem cleanup
    dirs_to_check = {
        "queue": QUEUE,
        "processing": PROCESSING,
        "done": DONE,
        "error": ERROR,
    }
    
    for dir_name, dir_path in dirs_to_check.items():
        if not dir_path.exists():
            continue
        
        test_files = sorted(dir_path.glob("test-sequential-job-*.json"))
        if test_files:
            print(f"\nFound {len(test_files)} test job file(s) in {dir_name}/:")
            for fpath in test_files:
                print(f"  - {fpath.name}")
            
            if not dry_run:
                for fpath in test_files:
                    fpath.unlink()
                print(f"✓ Deleted {len(test_files)} file(s) from {dir_name}/")
                removed_count += len(test_files)
            else:
                print(f"[DRY-RUN] Would delete {len(test_files)} file(s)")
    
    return removed_count


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    
    if dry_run:
        print("=== DRY-RUN MODE ===")
        print("No changes will be made. Use --apply to actually delete.\n")
    else:
        print("=== APPLYING DELETIONS ===\n")
    
    count = cleanup_test_jobs(dry_run=dry_run)
    
    if dry_run:
        print(f"\nTotal items to delete: {count}")
        print("Re-run with --apply to confirm deletion")
    else:
        print(f"\nTotal items deleted: {count}")
