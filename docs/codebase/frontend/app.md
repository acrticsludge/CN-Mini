# `frontend/app.py` — dashboard backend

Flask + Flask-SocketIO (`async_mode='threading'`). Serves the page and every
`/api/*` endpoint; syncs the bridge's JSON files into an in-memory
`transfer_state` the UI polls.

## State & helpers

- `transfer_state` — `running, filename, request_id, progress, chunks_sent,
  total_chunks, throughput, start_time, recovery_time, result, completed_at`.
- `_read_json` (tolerant) / `_write_json_atomic` (tmp + replace — the bridge
  must never read a half-written request).
- `_new_request_id()` — unique id per click (`ms-pid-counter`), minted under
  the start lock. A result can only ever settle the run that caused it.
- `_matches_current(doc)` — progress/result belongs to the queued run if
  `request_id` matches; falls back to filename so files from an older bridge
  (no id) are adopted, not orphaned.
- `_bridge_busy()` — three signals: pending `REQUEST_FILE`, fresh
  `running: True` progress (stale >600 s ignored), or our own un-resulted
  recent queue (covers the pickup window where the bridge has consumed the
  request but written nothing yet; presumed dead after `PENDING_GRACE`=120 s).
- `_sync_transfer_state()` — folds progress (computes **progress % from
  chunk/total** — the bar reads this, and it used to sit at 0 all run),
  result (stamps `completed_at` on first sight, `progress=100` on done), and
  events (ingested **by index**, resetting the cursor when the file shrinks —
  deduping by string used to swallow real repeated lines).
- `_bridge_status()` — liveness from `bridge_alive.json` (alive = touched
  within 10 s) for the dashboard indicator.
- `_start_lock` — serialises concurrent POSTs so two clicks can't both pass
  the busy check and both write.

## Routes

- `/` — the page. `/api/files` — `stored_files/h3|h4` listing (server-side
  proof of receipt). `/api/browse` — shared-files picker, hiding internal
  bookkeeping (`transfer_*`, `bridge.lock`, heartbeats, link files).
- `/api/transfer-result` — raw latest result file (the UI keys notices on it).
- `/api/start-transfer` — validate name → require file → lock → sync → **409
  if busy** (a second request would overwrite the live run's files) → clear
  stale bookkeeping → atomic request write with id → reset state → `queued`.
- `/api/stop-transfer` — drops a cancel file the bridge honors; marks stopped.
- `/api/link-down|up` → `_request_link` — writes id-stamped link request,
  waits ≤5 s for Mininet's echoing confirmation: confirmed → applied;
  rejected → 502; silence → 504 ("is topology.py running?"). The UI must
  reflect the real network, not intent.
- `/api/status` — synced transfer + event tail + bridge liveness (the poll
  workhorse). `/api/metrics` — syncs first, then throughput/recovery/progress
  (it once returned stale zeros because it never synced). `/api/bridge` —
  liveness alone.

## Pitfalls (hit during build)

- Every 409-versus-clobber, 504-versus-lying, index-versus-dedupe choice above
  was a real user-facing bug first ("works 10% of clicks", phantom failures).
- `SECRET_KEY` from env, dev fallback — fine for a classroom LAN demo.
