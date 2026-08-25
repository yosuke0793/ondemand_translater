#!/usr/bin/env python3
"""Delete all jobs from database and filesystem."""

import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path.home() / "translate_service"
DB_PATH = BASE / "jobs.db"
QUEUE = BASE / "queue"
PROCESSING = BASE / "processing"
DONE = BASE / "done"
ERROR = BASE / "error"
JOBS = BASE / "jobs"


def purge_all_jobs(dry_run: bool = True) -> None:
    """Remove all jobs from database and filesystem."""
    
    print("=" * 60)
    print("PURGE ALL JOBS" + (" [DRY-RUN]" if dry_run else ""))
    print("=" * 60)
    
    # Database cleanup
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM jobs")
            total_count = cursor.fetchone()[0]
            
            if total_count == 0:
                print("\n✓ Database is empty (no jobs to delete)")
            else:
                cursor = conn.execute("SELECT job_id, status FROM jobs ORDER BY created_at DESC")
                rows = cursor.fetchall()
                
                print(f"\nFound {total_count} job(s) in database:")
                for job_id, status in rows:
                    print(f"  - {job_id} ({status})")
                
                if not dry_run:
                    conn.execute("DELETE FROM jobs")
                    conn.commit()
                    print(f"\n✓ Deleted {total_count} job(s) from database")
                else:
                    print(f"\n[DRY-RUN] Would delete {total_count} job(s)")
        finally:
            conn.close()
    else:
        print("\n✓ Database does not exist")
    
    # Filesystem cleanup
    dirs_to_check = {
        "queue": QUEUE,
        "processing": PROCESSING,
        "done": DONE,
        "error": ERROR,
    }
    
    total_files = 0
    for dir_name, dir_path in dirs_to_check.items():
        if not dir_path.exists():
            continue
        
        job_files = sorted(dir_path.glob("*.json"))
        if job_files:
            print(f"\nFound {len(job_files)} job file(s) in {dir_name}/:")
            for fpath in job_files:
                print(f"  - {fpath.name}")
            
            if not dry_run:
                for fpath in job_files:
                    fpath.unlink()
                print(f"✓ Deleted {len(job_files)} file(s) from {dir_name}/")
            else:
                print(f"[DRY-RUN] Would delete {len(job_files)} file(s)")
            
            total_files += len(job_files)
    
    # jobs/ directory cleanup
    if JOBS.exists():
        job_dirs = [d for d in JOBS.iterdir() if d.is_dir()]
        if job_dirs:
            print(f"\nFound {len(job_dirs)} job director(ies) in jobs/:")
            for job_dir in job_dirs:
                print(f"  - {job_dir.name}/")
            
            if not dry_run:
                for job_dir in job_dirs:
                    shutil.rmtree(job_dir)
                print(f"✓ Deleted {len(job_dirs)} director(ies) from jobs/")
            else:
                print(f"[DRY-RUN] Would delete {len(job_dirs)} director(ies)")
            
            total_files += len(job_dirs)
    
    print("\n" + "=" * 60)
    if dry_run:
        print(f"Total items to delete: {total_files}")
        print("Re-run with --apply to confirm deletion")
    else:
        print(f"✓ Purge complete: {total_files} items deleted")
    print("=" * 60)


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    
    if dry_run:
        print("\n⚠️  DRY-RUN MODE: No changes will be made.\n")
        purge_all_jobs(dry_run=True)
        print("\n💡 To apply deletion, run:")
        print("   .venv/bin/python bin/purge_all_jobs.py --apply\n")
    else:
        response = input("⚠️  Are you sure? This will DELETE ALL JOBS. Type 'yes' to confirm: ").strip()
        if response.lower() == "yes":
            purge_all_jobs(dry_run=False)
            print("\n✓ All jobs have been deleted!\n")
        else:
            print("Cancelled.\n")
            sys.exit(0)
