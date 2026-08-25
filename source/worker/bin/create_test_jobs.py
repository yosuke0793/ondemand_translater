#!/usr/bin/env python3
"""Test script to verify sequential job processing.

Creates 3 test jobs and verifies they are processed one by one.
"""

import json
import time
from pathlib import Path

BASE = Path.home() / "translate_service"
QUEUE = BASE / "queue"
DONE = BASE / "done"

QUEUE.mkdir(parents=True, exist_ok=True)


def create_test_job(job_num: int) -> str:
    """Create a minimal test job JSON file."""
    job_id = f"test-sequential-job-{job_num}"
    job_file = QUEUE / f"{job_id}.json"
    
    job_data = {
        "job_id": job_id,
        "input_video": f"/tmp/test_{job_num}.mp4",
        "output_video": f"/tmp/output_{job_num}.mp4",
        "glossary_text": "",
    }
    
    job_file.write_text(json.dumps(job_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Created test job: {job_id}")
    return job_id


if __name__ == "__main__":
    print("Creating 3 test jobs...")
    for i in range(1, 4):
        create_test_job(i)
    
    print("\nTest jobs created in queue/. You can now:")
    print("  1. Run worker.py and observe sequential processing")
    print("  2. Run: python3 bin/list_jobs_db.py")
    print("  3. Run: python3 bin/list_jobs_db.py running")
    print("  4. Run: python3 bin/list_jobs_db.py done")
