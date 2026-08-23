#!/usr/bin/env python3
"""Inspect the contents of jobs.db.

This script prints:
- database path
- tables
- schema for each table
- row counts
- sample rows (configurable)

Usage:
  python3 bin/inspect_jobs_db.py
  python3 bin/inspect_jobs_db.py --table jobs --limit 20
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

BASE = Path.home() / "translate_service"
DB_PATH = BASE / "jobs.db"


def connect_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        raise SystemExit(1)
    return sqlite3.connect(str(DB_PATH))


def fetch_tables(conn: sqlite3.Connection) -> list[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in cursor.fetchall()]


def fetch_schema(conn: sqlite3.Connection, table_name: str) -> list[tuple]:
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return cursor.fetchall()


def fetch_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    cursor = conn.execute(f"SELECT COUNT(*) FROM {table_name}")
    return int(cursor.fetchone()[0])


def fetch_rows(conn: sqlite3.Connection, table_name: str, limit: int) -> tuple[list[str], list[tuple]]:
    cursor = conn.execute(f"SELECT * FROM {table_name} LIMIT ?", (limit,))
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    return columns, rows


def format_value(value) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    if len(text) > 80:
        return text[:77] + "..."
    return text


def print_table_section(conn: sqlite3.Connection, table_name: str, limit: int) -> None:
    print(f"\n== {table_name} ==")

    schema = fetch_schema(conn, table_name)
    print("Schema:")
    for cid, name, col_type, notnull, default_value, pk in schema:
        print(
            f"  - {name:<18} type={col_type or 'TEXT':<10} notnull={notnull} pk={pk} default={default_value}"
        )

    row_count = fetch_row_count(conn, table_name)
    print(f"Row count: {row_count}")

    if row_count == 0:
        print("No rows.")
        return

    columns, rows = fetch_rows(conn, table_name, limit)
    print(f"Sample rows (up to {limit}):")
    header = " | ".join(columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print(" | ".join(format_value(value) for value in row))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect jobs.db contents")
    parser.add_argument("--table", help="Inspect only one table")
    parser.add_argument("--limit", type=int, default=10, help="Max rows to show per table")
    args = parser.parse_args()

    with connect_db() as conn:
        print(f"Database: {DB_PATH}")
        tables = fetch_tables(conn)
        if not tables:
            print("No user tables found.")
            return

        print("Tables:")
        for table_name in tables:
            print(f"  - {table_name}")

        if args.table:
            if args.table not in tables:
                print(f"Table not found: {args.table}", file=sys.stderr)
                raise SystemExit(1)
            print_table_section(conn, args.table, args.limit)
            return

        for table_name in tables:
            print_table_section(conn, table_name, args.limit)


if __name__ == "__main__":
    main()
