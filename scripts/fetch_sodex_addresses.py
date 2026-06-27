#!/usr/bin/env python3
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple


BASE_URL = "https://sodex.dev/mainnet/chain/user/{user_id}/address"
DEFAULT_START_USER_ID = 1000
DEFAULT_CHECKPOINT_EVERY = 1000
DEFAULT_MAX_CONSECUTIVE_MISSES = 10
DEFAULT_MAX_RUNTIME_SECONDS = 25 * 60
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 0.06
REQUEST_TIMEOUT_SECONDS = 15
TRANSIENT_RETRIES = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Sodex user addresses into a resumable CSV."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Directory that stores the CSV and state file.",
    )
    parser.add_argument(
        "--start-user-id",
        type=int,
        default=int(os.getenv("START_USER_ID", DEFAULT_START_USER_ID)),
        help="Starting user ID if no saved state exists.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=int(os.getenv("CHECKPOINT_EVERY", DEFAULT_CHECKPOINT_EVERY)),
        help="Save progress every N attempted user IDs.",
    )
    parser.add_argument(
        "--max-consecutive-misses",
        type=int,
        default=int(
            os.getenv(
                "MAX_CONSECUTIVE_MISSES", DEFAULT_MAX_CONSECUTIVE_MISSES
            )
        ),
        help="Stop after this many consecutive 'User not found' responses.",
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=int(
            os.getenv("MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS)
        ),
        help="Stop gracefully once runtime reaches this limit.",
    )
    parser.add_argument(
        "--min-request-interval-seconds",
        type=float,
        default=float(
            os.getenv(
                "MIN_REQUEST_INTERVAL_SECONDS",
                DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
            )
        ),
        help="Minimum delay between request starts to stay under rate limits.",
    )
    return parser.parse_args()


def atomic_write_json(path: Path, payload: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def load_last_csv_user_id(csv_path: Path) -> Optional[int]:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None

    last_user_id: Optional[int] = None
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                last_user_id = int(row["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
    return last_user_id


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return data


def build_initial_state(
    state_path: Path, csv_path: Path, start_user_id: int
) -> dict:
    state = load_state(state_path)
    last_csv_user_id = load_last_csv_user_id(csv_path)

    next_user_id = state.get("next_user_id", start_user_id)
    try:
        next_user_id = int(next_user_id)
    except (TypeError, ValueError):
        next_user_id = start_user_id

    if last_csv_user_id is not None:
        next_user_id = max(next_user_id, last_csv_user_id + 1)

    total_saved_rows = 0
    if last_csv_user_id is not None:
        total_saved_rows = max(0, last_csv_user_id - start_user_id + 1)

    previous_total_saved_rows = state.get("total_saved_rows")
    try:
        previous_total_saved_rows = int(previous_total_saved_rows)
    except (TypeError, ValueError):
        previous_total_saved_rows = 0

    prev_consecutive_misses = int(state.get("consecutive_misses", 0) or 0)
    if prev_consecutive_misses > 0:
        next_user_id = max(start_user_id, next_user_id - prev_consecutive_misses)

    return {
        "next_user_id": next_user_id,
        "consecutive_misses": 0,
        "total_attempted": int(state.get("total_attempted", 0) or 0),
        "total_saved_rows": max(total_saved_rows, previous_total_saved_rows),
        "last_success_user_id": state.get("last_success_user_id"),
        "last_success_address": state.get("last_success_address"),
        "last_run_started_at": state.get("last_run_started_at"),
        "last_run_finished_at": state.get("last_run_finished_at"),
        "last_stop_reason": state.get("last_stop_reason"),
        "last_error": state.get("last_error"),
    }


def fetch_user_payload(user_id: int) -> Tuple[str, Optional[str]]:
    url = BASE_URL.format(user_id=user_id)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "sodex-reverse-search-fetcher/1.0",
        },
    )

    last_exception: Optional[Exception] = None
    for attempt in range(1, TRANSIENT_RETRIES + 1):
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exception = exc
            if attempt == TRANSIENT_RETRIES:
                raise
            time.sleep(1.5 * attempt)
    else:
        raise RuntimeError(f"Unexpected fetch failure: {last_exception}")

    code = payload.get("code")
    data = payload.get("data")

    if code == 0 and isinstance(data, dict):
        address = data.get("address")
        if isinstance(address, str) and address:
            return "found", address
        return "error", "Missing address in success payload"

    if code == 404 and data is None:
        return "missing", None

    return "error", f"Unexpected payload: {json.dumps(payload, sort_keys=True)}"


def ensure_csv_has_header(csv_path: Path) -> None:
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["user_id", "address"])


def save_checkpoint(
    state_path: Path,
    state: dict,
    run_started_at: str,
    stop_reason: str,
    error_message: Optional[str] = None,
) -> None:
    state["last_run_started_at"] = run_started_at
    state["last_run_finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["last_stop_reason"] = stop_reason
    state["last_error"] = error_message
    atomic_write_json(state_path, state)


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    csv_path = data_dir / "addresses.csv"
    state_path = data_dir / "state.json"

    ensure_csv_has_header(csv_path)
    state = build_initial_state(state_path, csv_path, args.start_user_id)
    run_started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started_at = time.monotonic()

    print(
        f"Starting from user_id={state['next_user_id']} "
        f"with consecutive_misses={state['consecutive_misses']}"
    )

    processed_this_run = 0
    saved_this_run = 0
    last_request_started_at = 0.0

    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)

        while state["consecutive_misses"] < args.max_consecutive_misses:
            elapsed = time.monotonic() - started_at
            if elapsed >= args.max_runtime_seconds:
                handle.flush()
                save_checkpoint(
                    state_path,
                    state,
                    run_started_at,
                    stop_reason="max_runtime_reached",
                )
                print(
                    "Stopped because max runtime was reached. "
                    f"Next user_id={state['next_user_id']}"
                )
                return 0

            now = time.monotonic()
            sleep_for = args.min_request_interval_seconds - (now - last_request_started_at)
            if sleep_for > 0:
                time.sleep(sleep_for)

            current_user_id = state["next_user_id"]
            last_request_started_at = time.monotonic()

            try:
                status, detail = fetch_user_payload(current_user_id)
            except Exception as exc:  # noqa: BLE001
                handle.flush()
                save_checkpoint(
                    state_path,
                    state,
                    run_started_at,
                    stop_reason="transient_error",
                    error_message=str(exc),
                )
                print(
                    f"Stopped on transient error at user_id={current_user_id}: {exc}",
                    file=sys.stderr,
                )
                return 0

            state["total_attempted"] += 1
            processed_this_run += 1

            if status == "found":
                writer.writerow([current_user_id, detail])
                state["consecutive_misses"] = 0
                state["total_saved_rows"] += 1
                state["last_success_user_id"] = current_user_id
                state["last_success_address"] = detail
                saved_this_run += 1
            elif status == "missing":
                state["consecutive_misses"] += 1
            else:
                handle.flush()
                save_checkpoint(
                    state_path,
                    state,
                    run_started_at,
                    stop_reason="unexpected_payload",
                    error_message=detail,
                )
                print(
                    f"Stopped on unexpected payload at user_id={current_user_id}: {detail}",
                    file=sys.stderr,
                )
                return 0

            state["next_user_id"] = current_user_id + 1

            if processed_this_run % args.checkpoint_every == 0:
                handle.flush()
                save_checkpoint(
                    state_path,
                    state,
                    run_started_at,
                    stop_reason="checkpoint_saved",
                )
                print(
                    f"Checkpoint saved after {processed_this_run} processed this run. "
                    f"Next user_id={state['next_user_id']}, "
                    f"saved_rows_this_run={saved_this_run}, "
                    f"consecutive_misses={state['consecutive_misses']}"
                )

        handle.flush()

    save_checkpoint(
        state_path,
        state,
        run_started_at,
        stop_reason="max_consecutive_misses_reached",
    )
    print(
        "Stopped because 10 consecutive missing users were reached. "
        f"Final next_user_id={state['next_user_id']}, "
        f"total_saved_rows={state['total_saved_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
