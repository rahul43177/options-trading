# SQLite schema

`data/live_option_chain/delta_live.sqlite` is portable SQLite. `option_snapshots` has primary key `(snapshot_ts_us, symbol)` and retains the complete raw API object in `raw_json`, so derived fields remain auditable. `collector_runs` links each snapshot to immutable raw JSON. The remaining tables hold paper positions/orders/fills, account snapshots, decisions, and risk events.

The collector uses UTC Unix microseconds and idempotent inserts, so retries cannot duplicate a snapshot.
