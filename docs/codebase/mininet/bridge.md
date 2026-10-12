# `mininet/bridge.py` — the h1 agent

Runs **on h1 inside Mininet**. Watches `transfer_request.json`, runs the TCP
client as a subprocess per transfer, and streams machine-readable progress /
results back out as JSON files. It is the only link between the dashboard
(host namespace) and the data plane — plain files, no RPC.

## File protocol (`SHARED_DIR` = `/tmp/shared` in Mininet, repo fallback)

| File | Direction | Content |
|---|---|---|
| `transfer_request.json` | dashboard → bridge | `{filename, request_id}` |
| `transfer_result.json` | bridge → dashboard | `{filename, request_id, status, error?}` |
| `transfer_progress.json` | bridge → dashboard | `{filename, request_id, running, chunk, total, throughput, …}` |
| `transfer_events.json` | bridge → dashboard | append-only list (capped at 50) |
| `transfer_cancel.json` | dashboard → bridge | presence = kill the running client |
| `bridge.lock` | singleton | pidfile; freshness = liveness |
| `bridge_alive.json` | bridge → dashboard | `{pid, started_at, last_poll, servers}` heartbeat |

## Functions

- `claim_bridge_lock(max_age=10)` — `O_EXCL` create; a lock touched within
  10 s means a live rival → return `False` (newcomer exits loudly instead of
  racing: duplicate bridges = duplicate transfers + interleaved results, the
  "stored but UI says failed" symptom). Older = corpse → reclaim. Liveness is
  file freshness, never a pid probe (portable Linux/Windows).
- `write_heartbeat(started_at=None)` — writes `bridge_alive.json` and touches
  the lock. Called every idle poll **and every ~2 s from inside `run_client`**:
  without the latter, a multi-minute transfer starves the heartbeat and the
  dashboard declares the bridge dead mid-run.
- `report_namespace()` — derives h1's IP via a UDP `connect` trick and warns
  unless it is `10.0.0.1`. A mislaunched bridge is visible in one log line
  instead of twenty failed connects.
- `main()` — claims the lock (exit 1 with an explanatory message otherwise),
  reports namespace, then polls: drop corrupt requests (never retry poison),
  unlink → `process_request` → stamp + write result + event. Heartbeats each
  idle iteration.
- `process_request(request)` — validates filename (no traversals), requires
  the file to exist, clears bookkeeping (**never the request itself** — that
  race ate clicks), writes a running pickup marker *before* the client runs
  (closes the busy-check blind window), runs, echoes `request_id` in the result.
- `run_client(filename, filepath, request_id)` — spawns the client with
  `--transfer-timeout` from `BRIDGE_TRANSFER_TIMEOUT` (default 60 s — must
  cover STP reconvergence, see `client.md`), parses stdout:
  `[PROGRESS]` → progress file (+ remembers last values),
  `[COMPLETE]` → ACK-tally event, `[CLIENT]` stage/failure lines → events
  (last failure kept as the reason). Honors cancel file (SIGTERM → `stopped`)
  and a configurable deadline. Terminal progress carries the **last live
  values** (otherwise metrics zero at completion) with `running: False`.
  Returns `(status, error)`; `done` requires exit 0 **and** `[COMPLETE]`.
- `clear_transfer_files()` / `_write_terminal_progress()` / `emit_event()` /
  `_write_json()` — small helpers; all writes atomic (`tmp` + `os.replace`).

## Pitfalls (hit during build)

- `[STORED]` is printed by the server; the bridge reads the *client's*
  stdout, so waiting for it would hang every transfer. Removed; don't re-add.
- Exit code alone once certified failures as successes (client exited 0
  without connecting). Both signals required — see `client.md`.
