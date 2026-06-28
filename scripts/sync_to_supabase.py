#!/usr/bin/env python3
"""Sync new CSV rows to Supabase sodex_addresses table."""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Tuple


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
TABLE_NAME = "sodex_addresses"
BATCH_SIZE = 500
REQUEST_TIMEOUT_SECONDS = 30
TRANSIENT_RETRIES = 3


def load_sync_state(state_path: Path) -> int:
    if not state_path.exists():
        return 0
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return int(data.get("last_synced_user_id", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def save_sync_state(state_path: Path, last_user_id: int) -> None:
    state_path.write_text(
        json.dumps({"last_synced_user_id": last_user_id}, indent=2) + "\n",
        encoding="utf-8",
    )


def read_new_rows(csv_path: Path, last_synced: int) -> Tuple[List[dict], int]:
    rows: List[dict] = []
    max_user_id = last_synced
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                uid = int(row["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if uid <= last_synced:
                continue
            rows.append({"user_id": uid, "address": row["address"]})
            max_user_id = max(max_user_id, uid)
    return rows, max_user_id


def upsert_batch(rows: List[dict]) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}?on_conflict=user_id"
    payload = json.dumps(rows).encode("utf-8")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    for attempt in range(1, TRANSIENT_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                resp.read()
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            if attempt == TRANSIENT_RETRIES:
                raise
            print(f"  Retry {attempt}/{TRANSIENT_RETRIES} after error: {exc}")
            time.sleep(2 * attempt)


def main() -> int:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY env vars", file=sys.stderr)
        return 1

    data_dir = Path("data")
    csv_path = data_dir / "addresses.csv"
    sync_state_path = data_dir / "sync_state.json"

    if not csv_path.exists():
        print("No addresses.csv found", file=sys.stderr)
        return 1

    last_synced = load_sync_state(sync_state_path)
    print(f"Last synced user_id: {last_synced}")

    rows, max_user_id = read_new_rows(csv_path, last_synced)
    if not rows:
        print("No new rows to sync.")
        return 0

    print(f"Syncing {len(rows)} rows to Supabase in batches of {BATCH_SIZE}...")

    total_upserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        upsert_batch(batch)
        total_upserted += len(batch)
        batch_max = batch[-1]["user_id"]
        save_sync_state(sync_state_path, batch_max)
        print(f"  Batch {i // BATCH_SIZE + 1}: upserted {total_upserted}/{len(rows)} (up to user_id={batch_max})")

    print(f"Sync complete. {total_upserted} rows upserted. Last user_id={max_user_id}")
    save_sync_state(sync_state_path, max_user_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
