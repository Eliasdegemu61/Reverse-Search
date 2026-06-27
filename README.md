# Sodex Reverse Search

This repo builds and keeps updating a simple CSV table of Sodex user IDs and EVM addresses for reverse-search use.

## What it does

- Starts at `userId` `1000`
- Calls `https://sodex.dev/mainnet/chain/user/{userId}/address`
- Saves successful results into `data/addresses.csv`
- Uses exactly 2 CSV columns: `user_id,address`
- Stops only after `10` consecutive `"User not found"` responses
- Continues from the last saved `userId` on every next run
- Saves a checkpoint every `1000` attempted IDs

## Files

- `data/addresses.csv`: the address table for your website
- `data/state.json`: saved progress so the job can resume safely
- `scripts/fetch_sodex_addresses.py`: the fetcher
- `.github/workflows/reverse-search.yml`: the GitHub Actions cron job

## GitHub Actions behavior

- Runs every 30 minutes
- Also supports manual runs with `workflow_dispatch`
- Each run processes for up to 25 minutes, then exits cleanly
- Commits the updated CSV and state back into the repo
- Uses a concurrency lock so two cron runs do not overlap

This means the first backfill can take multiple scheduled runs, but it will keep continuing from where it left off until the dataset is complete. After that, each 30-minute run will only check newer user IDs.

## Local run

```bash
python scripts/fetch_sodex_addresses.py
```

Optional environment variables:

- `START_USER_ID`
- `CHECKPOINT_EVERY`
- `MAX_CONSECUTIVE_MISSES`
- `MAX_RUNTIME_SECONDS`
- `MIN_REQUEST_INTERVAL_SECONDS`

## Notes

- The Sodex endpoint returns `200 OK` even when a user does not exist. In that case the JSON payload is:
  - `{"code":404,"message":"User not found","data":null}`
- The script treats that as a missing user and counts it toward the 10 consecutive misses rule.
