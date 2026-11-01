# Server Failure Recovery — Implementation Spec

**Project:** CN-Mini, Distributed Backup System
**Edge case:** #1, server dies mid-transfer
**Status:** Implemented on `edge-case-1`
**Date:** 2026-10-31
**Related:** `docs/edge-cases.md` (case 1), `docs/architecture.md`, `docs/codebase/`

---

## 1. Goal

A transfer survives the backup server dying halfway through. The client stops, the bridge waits for the server to return, and the transfer continues from the last chunk the server acknowledged. If the server doesn't return within the retry window, the run restarts on the backup server.

The dashboard makes this visible the same way link failure is visible today: a button in Transfer Control, a state change in the topology drawing, and event-log lines with their own colors.

### Non-goals

- Cross-server resume. Failing over to h4 restarts from chunk 0. True replication is future work.
- Surviving a full host power-off. The demo kills the server process, which closes the socket. A network blackhole still uses the 60 s ACK timeout.
- Changing the SDN controller. The controller keeps doing what it does for link failure.

---

## 2. User story

> During a backup to h3, the server dies. The dashboard dims h3 to grey, keeps the route visible as a paused dotted line, and says "h3 unreachable, retrying 1/2". I restart h3. The transfer continues from chunk 40, h3 flashes green, and the file finishes with the same hash. If I leave h3 down, the bridge restarts the backup on h4, h4 gets the blue active border, the dotted line follows the h4 route, and the bar moves again.

A second story covers the button:

> With no transfer running, I press "Kill Server h3". h3 turns red in the topology. I press "Restart h3" and it turns green. No transfer is affected.

---

## 3. Current behavior

| Step         | Today                                                                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Server dies  | The client's `recv_frame` returns `None`. `_send_chunks` sets `last_ack = chunk_index - 1`, prints `[CLIENT] No ACK received...`, returns `False`. |
| Client exits | `sys.exit(1)`. The bridge sees exit 1 and no `[COMPLETE]`, so the result is `failed`.                                                              |
| Bridge       | Runs the client once. No retry.                                                                                                                    |
| Partial file | Survives in `stored_files/h3/` because `/tmp/backup` is bind-mounted.                                                                              |
| New attempt  | A fresh Start sends `resume_from=0`, and the server opens the output with `wb`, truncating the partial file.                                       |
| Dashboard    | Shows a generic failed result after the client exits. No server state, no retry text.                                                              |

The protocol already supports resume: `HELLO` carries `resume_from`, and `_handle_hello` seeks to that chunk. Nothing calls it.

---

## 4. Target behavior

### 4.1 State machine

```
idle -> queued -> running -> retrying -> resuming -> running -> done
                              |              |
                              |              +-> failed
                              +-> failover -> running -> done
                              +-> stopped
running -> failed
running -> stopped
```

| State                         | Meaning                                         | `running` in progress file |
| ----------------------------- | ----------------------------------------------- | -------------------------- |
| `queued`                      | Request written, bridge hasn't picked it up     | false                      |
| `running`                     | Client is sending chunks                        | true                       |
| `retrying`                    | Client failed; bridge waits for the same server | true                       |
| `resuming`                    | Client reconnected with `--resume N`            | true                       |
| `failover`                    | Bridge restarts the file on h4 from 0           | true                       |
| `done` / `failed` / `stopped` | Terminal                                        | false                      |

### 4.2 Timing

| Constant                | Value | Why                                                         |
| ----------------------- | ----- | ----------------------------------------------------------- |
| `SERVER_PROBE_ATTEMPTS` | 2     | Two probes before consulting server process state                      |
| `SERVER_PROBE_INTERVAL` | 5 s   | Gap between the two probes to `host:9000`                             |
| `LINK_RECOVERY_WAIT`    | 60 s  | Extra bounded wait when TCP fails but h3 is still alive               |
| `SERVER_STATUS_MAX_AGE` | 10 s  | Older server-process state is unknown, not evidence of death        |
| `HEARTBEAT_INTERVAL`    | 2 s   | Fixed bridge heartbeat during client output stalls                   |
| `RETRY_ATTEMPTS`        | 2     | One same-server retry, then one failover                    |
| `CLIENT_TIMEOUT`        | 300 s | Unchanged; per client attempt                               |
| `TRANSFER_TIMEOUT`      | 60 s  | Unchanged; covers STP and TCP retransmission                |

The bridge probes the same server twice, 5 s apart. A probe that succeeds cancels the failover and retries with `--resume N`. If both probes fail, the bridge reads fresh `server_status.json`: an alive h3 means the path may still be converging, so it waits up to 60 s for TCP to recover instead of declaring the server dead. Only a dead/stale process, a newly dead process, or expiry of the path wait starts the h4 attempt with `--resume 0`.

A cancel file stops the wait within 0.5 s.

---

## 5. Backend design

### 5.1 Protocol changes

No new message type is required. Two existing fields do the work.

**Ready ACK carries the accepted start chunk.** Today the server sends `ACK(0)` after `HELLO` and the client ignores the payload. Change the server to send `ACK(accepted_start)`:

- `resume_from == 0`: open `wb`, accepted 0.
- `resume_from > 0` and the partial file exists with at least `resume_from * CHUNK_SIZE` bytes: open `r+b`, seek, accepted `resume_from`.
- `resume_from > 0` and no usable partial file: open `wb`, accepted 0. The client restarts from 0 on that server.

The client reads the ready ACK payload and uses it as `start_chunk`, overriding its requested resume point when the server can't honour it. This makes an accidental failover with `--resume N` safe: h4 replies 0, and the client starts at 0 instead of writing chunk N at offset 0.

**Client resume point line.** On any chunk failure, the client prints:

```
[RESUME_POINT] {"last_ack": 39, "next_chunk": 40, "total": 128, "server": "10.0.0.3:9000"}
```

`next_chunk` is the first chunk the server has not acknowledged. The bridge uses that value directly. The line is printed before the client returns `False`.

### 5.2 `client/client.py`

- `send_file`:
  - After the ready ACK, read `accepted_start = unpack_ack(ack_frame['payload'])` (or the existing `struct.unpack`). If it's a sane value between 0 and `total_chunks`, set `start_chunk = accepted_start`.
  - If the server accepts a lower start than requested, print `[CLIENT] Server accepted resume from chunk {accepted_start}` and start there.
  - Keep the separate `RESUME` frame branch for compatibility. The bridge path doesn't use it; a fresh process has `last_ack = -1`.
- `_send_chunks`:
  - On `sendall` failure, missing ACK, or a `recv_frame` timeout, print `[RESUME_POINT]` with `last_ack` and `next_chunk = last_ack + 1`.
  - Keep the existing `[CLIENT] Connection lost at chunk N` line. The bridge parses the structured line.
  - On a successful finish, keep `[COMPLETE]`.
- CLI: no new flags. `--resume N` already exists.
- Source consistency check (case 6 in `edge-cases.md`) is separate. Don't add it here.

### 5.3 `server/server.py`

The server must write each chunk at its own index, not at the current file position. Without this, a retry after a lost ACK writes the same data at the next offset and corrupts the file.

The server writes to `<filename>.part` and promotes it with `os.replace` to `<filename>` only after the final chunk. A crash leaves the previous final copy untouched, and `/api/files` skips `.part` so an interrupted transfer is not listed as a stored backup. The promotion happens before the final ACK, so a client that sees the ACK also sees the final file.

- `ClientHandler`:
  - Replace `received_chunks` count with `self.received = set()`.
  - `_handle_hello`:
    - Compute `total_chunks`.
    - Determine `accepted_start` as above.
    - Open `part_path` `r+b` when resuming, `wb` otherwise.
    - If resuming, `received = set(range(accepted_start))`. The client's resume point is trusted for the prefix.
    - Send `pack_ack(accepted_start)`, not `pack_ack(0)`.
    - If `file_size == 0`, promote the empty part and print `[STORED]` before the ready ACK.
  - `_handle_data`:
    - If `frame['index'] >= total_chunks`, drop it (bad index).
    - If `frame['index'] in self.received`, send `ACK(index)` and return. This makes a retry after a lost ACK idempotent.
    - Verify CRC. On mismatch, drop without an ACK, as today.
    - Seek to `index * CHUNK_SIZE`, write, flush, add to `received`.
    - On the final chunk, truncate to `file_size`, close, `os.replace(part_path, output_path)`, print `[STORED]`, then send `ACK(index)`.
  - `_handle_resume`: keep it. If the bridge ever sends a `RESUME` frame, set `accepted_start = last_ack + 1`, seek, and `received = set(range(accepted_start))`; ACK `last_ack`.
- `BackupServer`: no change.

### 5.4 `mininet/bridge.py`

`run_client` becomes a single attempt that returns a result dict:

```python
{
  'status': 'done' | 'failed' | 'stopped',
  'error': str | None,
  'resume_point': int | None,   # next_chunk from [RESUME_POINT]
  'server': 'h3' | 'h4' | None,
  'chunks': int,
}
```

- Parse `[RESUME_POINT]` from stdout. Parse `[CLIENT] Connected to <host>:<port>` to set `server`.
- `process_request` owns the retry loop:

```
attempt 1: default servers [h3, h4], resume 0
  on failed + resume_point:
    write phase=retrying, attempt=1, resume_from=resume_point, server=h3
    probe h3:9000 twice, 5 s apart (cancel-aware)
    if up: attempt 2 with --servers h3 --resume resume_point, phase=resuming
    if down and fresh server_status.json says h3 is alive:
      wait up to 60 s for the path, honoring Stop and h3 death;
      if the path recovers, attempt 2 with --servers h3 --resume resume_point
    else: attempt 2 with --servers h4 --resume 0, phase=failover
  on failed without resume_point: final failed
  on done: final done
attempt 2:
  on done: done, attempts=2, restarted_on_backup=(server == h4 and resume_from == 0)
  on failed: final failed
```

- Progress file during the wait:
  ```json
  {
    "filename": "big.bin",
    "request_id": "...",
    "running": true,
    "chunk": 40,
    "total": 128,
    "throughput": 0.0,
    "phase": "retrying",
    "attempt": 1,
    "max_attempts": 2,
    "resume_from": 40,
    "server": "h3",
    "retry_wait_s": 4.2
  }
  ```
- Every `[PROGRESS]` write carries `phase: "running"`, `attempt`, `max_attempts`, `resume_from`, and `server`. The transient `retrying`/`resuming`/`failover` phase is written before the attempt starts; the first chunk switches it to `running` so the bar moves and the topology keeps the right server.
- Cancel: check `transfer_cancel.json` every 0.5 s during the wait. Write terminal progress with `cancelled: true`, `phase: "stopped"`, and return `stopped`. Do not start a new client.
- Terminal progress: keep the last `chunk`, `total`, `throughput`, `server`, and `resume_from`, as `_write_terminal_progress` does today.
- Result additions: `attempts`, `resumed_from`, `server`, `restarted_on_backup`, `downtime_s`.
- Deadline: each client attempt gets its own 300 s. The two probes and a possible 60 s path wait are outside that budget. The whole run is still bounded by the two attempts plus those waits.

### 5.5 Server control inside Mininet

Add `mininet/serverctl.py`, separate from `topology.py` so it can be unit-tested with a fake `net`.

**Process handles, not `pkill`.** Mininet hosts share the host PID namespace. `pkill -f server.py` run inside h3 can kill h4's server too. Store the `Popen` object returned by `node.popen()` for each server and use `proc.terminate()` / `proc.kill()`. If you must use `pkill`, match the full command including `--host 10.0.0.3`.

**API:**

- `start_server(net, host) -> Popen`
  - `node = net.get(host)`, `ip = {'h3': '10.0.0.3', 'h4': '10.0.0.4'}[host]`.
  - Open `/tmp/server_<host>.log` in append mode.
  - `node.popen([sys.executable, '/tmp/src/server/server.py', '--host', ip, '--port', '9000', '--storage', '/tmp/backup'], stdout=log, stderr=STDOUT, env=...)`.
- `apply_server_state(net, procs, host, action) -> (ok, message, state)`
  - Validate `host in ('h3', 'h4')` and `action in ('down', 'up')`.
  - `down`: if the process is missing or already exited, return `(True, 'already down', 'down')`. Otherwise `terminate()`, `wait(timeout=5)`, `kill()` if needed, return `(True, 'h3 down', 'down')`.
  - `up`: if alive, return `(True, 'already up', 'up')`. Otherwise `procs[host] = start_server(...)` and return `(True, 'h3 up', 'up')`.
- `server_status(procs) -> dict`
  - `{host: {'alive': bool, 'pid': int|None, 'since': str}}`.
- `serve_server_requests(net, procs, request_file, result_file, status_file, poll=0.3, should_continue=None)`
  - On each tick, write `server_status.json` atomically even when the values are unchanged, so its mtime acts as a heartbeat. Include a `checked_at` field.
  - If `server_request.json` exists, read it, unlink, apply, write `server_result.json` with the same `request_id`.
  - Detect an unexpected exit (`proc.poll() is not None` when it was alive) and update the status. Log `[SERVERCTL] h3 exited unexpectedly`.
  - Run in a daemon thread so the main thread keeps serving link requests.

**`mininet/topology.py` changes:**

- Store `server_procs[host] = node.popen(...)` in the launch loop. Refactor the launch body into `start_server` so the initial start and the restart use the same command.
- Start the server watcher thread after the servers launch.
- Clear `server_request.json`, `server_result.json`, and `server_status.json` on boot.
- In the `finally`, terminate the server processes before `net.stop()` so a restart doesn't accumulate zombies. The existing startup `pkill` stays as a backstop.

### 5.6 Flask API

**New files and routes:**

```python
SERVER_REQUEST_FILE = os.path.join(SHARED_DIR, 'server_request.json')
SERVER_RESULT_FILE = os.path.join(SHARED_DIR, 'server_result.json')
SERVER_STATUS_FILE = os.path.join(SHARED_DIR, 'server_status.json')
```

- `POST /api/server-down` with `{"host": "h3"}`.
- `POST /api/server-up` with `{"host": "h3"}`.
- Both call `_request_server(host, action)`, which mirrors `_request_link`:
  - Validate host in `('h3', 'h4')`, default `h3`.
  - Write `server_request.json` with a unique `request_id`.
  - Wait up to 5 s for `server_result.json` with the same `request_id` and matching host/action.
  - Confirmed and applied: return 200, log `[SERVER] h3 down confirmed`, emit `server_event`.
  - Confirmed but rejected: return 502 with the error.
  - No confirmation: return 504 with "is mininet/topology.py running?".
- `_server_status()` reads `server_status.json`. If the file is missing or older than 10 s, return `alive: None` (unknown), not `false`.
- `/api/status` adds:
  ```json
  {
    "transfer": { "...", "phase": "retrying", "attempt": 1,
                  "max_attempts": 2, "resume_from": 40,
                  "server": "h3", "retry_wait_s": 4.2 },
    "servers": { "h3": {"alive": false, "pid": 1234, "age_s": 1.2},
                 "h4": {"alive": true, "pid": 1235, "age_s": 1.2} },
    "events": [], "bridge": {}
  }
  ```
- `_sync_transfer_state()` copies the new progress fields into `transfer_state`.
- `/api/metrics` returns `recovery_time`, `recovery_kind` (`link` or `server`), `throughput`, and `progress`. The Metrics panel shows the latest value with its kind.
- Recovery clock:
  - Add `server_down_at`, `server_recovery_time`, `recovery_kind`.
  - When a server-down is confirmed during a running transfer, set `server_down_at = time.time()`.
  - When the first `phase: "retrying"` arrives for a run and `server_down_at` is unset, set it. This covers an automatic crash that didn't use the button.
  - When `chunk` advances after `server_down_at` and the 3 s straggler grace passes, set `server_recovery_time = round(now - server_down_at, 1)`, `recovery_kind = 'server'`, and log `[RECOVERY] Server h3 back after X.Xs; resumed at chunk N`.
  - The existing link recovery keeps `recovery_time` and `recovery_kind = 'link'`. The Metrics panel shows the latest value with its kind.
  - Reset both on a new transfer.

### 5.7 Side-channel contract

| File                     | Direction        | Shape                                                                                                                |
| ------------------------ | ---------------- | -------------------------------------------------------------------------------------------------------------------- |
| `server_request.json`    | Flask → topology | `{"host":"h3","action":"down","request_id":"..."}`                                                                   |
| `server_result.json`     | topology → Flask | `{"host":"h3","action":"down","applied":true,"state":"down","request_id":"...","message":"h3 down","at":"14:02:01"}` |
| `server_status.json`     | topology → Flask | `{"checked_at":"14:02:01","h3":{"alive":true,"pid":1234,"since":"14:00:00"},"h4":{...}}`                             |
| `transfer_progress.json` | bridge → Flask   | Existing fields plus `phase`, `attempt`, `max_attempts`, `resume_from`, `server`, `retry_wait_s`                     |
| `transfer_result.json`   | bridge → Flask   | Existing fields plus `attempts`, `resumed_from`, `server`, `restarted_on_backup`, `downtime_s`                       |

All writes stay atomic (`tmp` + `os.replace`), as they are today.

---

## 6. Frontend design

### 6.1 Buttons

Add two buttons to the Transfer Control panel, next to the link buttons:

```html
<button id="btn-server-down" class="btn btn-danger">Kill Server h3</button>
<button id="btn-server-up" class="btn btn-success">Restart h3</button>
```

- `btn-server-down` POSTs `/api/server-down` with `{host: 'h3'}`.
- `btn-server-up` POSTs `/api/server-up` with `{host: 'h3'}`.
- On a confirmed response, call `updateHostStatus('h3', 'down'|'up')` and add a local event line.
- Both buttons stay enabled. The API is idempotent and returns "already down" or "already up".
- A second tab sees the change on its next `/api/status` poll (2 s).

The control group has a native `h3 / h4` selector; h3 is the default. Both buttons read the selected host at click time.

### 6.2 Topology states

The SVG already has `#host-h1` … `#host-h4` and `#link-s1-s2` … `#link-s3-s4`. Add host classes:

| State              | Class                          | Visual                                                        |
| ------------------ | ------------------------------ | ------------------------------------------------------------- |
| Serving, inactive  | none (existing)                | Red stroke for servers, green for clients                     |
| Active destination | `.host.active-server`          | Blue stroke (#4ea8de) and glow; follows `state.server`        |
| Down               | `.host.down`                   | Grey, dim, dashed, no pulse; the route stays visible          |
| Recovering         | `.host.recovering`             | Green flash for 1.5 s, then the active blue if it is the target |
| Failover target    | `.host.recovering` + active    | h4 flashes, then keeps the blue active border                 |
| Unknown            | `.host.unknown`                | Dim grey; status file stale or missing                        |

Map the status payload to the class: `alive === true` becomes `up`, `alive === false` becomes `down`, and `alive === null` or missing becomes `unknown`.

`updateHostStatus(host, status)`:

```js
function updateHostStatus(host, status) {
  const el = document.getElementById("host-" + host);
  if (!el) return;
  const wasDown = el.classList.contains("down") || el.classList.contains("unknown");
  el.classList.remove("down", "recovering", "unknown");
  if (status === "down") {
    el.classList.remove("active-server");
    el.classList.add("down");
  } else if (status === "unknown") {
    el.classList.add("unknown");
  } else if (status === "up" && wasDown) {
    el.classList.add("recovering");
    setTimeout(() => el.classList.remove("recovering"), 1500);
  }
}
```

### 6.3 Packet-flow animation

The current animation is progress-driven. `renderFlow(state)` uses `chunks_sent / total` and a BFS path from `FLOW_SRC` to `FLOW_DST`. Extend it:

- Replace `const FLOW_DST = 'h3'` with `let flowDest = 'h3'`.
- `renderFlow(state)` sets `flowDest = state.server === 'h4' ? 'h4' : 'h3'` when `state.server` is present.
- Do not block the route when the destination host is down. The host node shows the disabled state; the path stays visible so the viewer can see which server failed.
- Draw one dashed `<path id="flow-line">` from the actual link endpoints, oriented from source to destination. Each link becomes a `M … L …` subpath, so the line follows the visible link rather than drifting center-to-center on diagonals. An amber arrowhead (`marker-end`) sits at the end of every segment, and `stroke-dashoffset` animates with `flow-dash` so the dashes move in the data direction.
- When `phase === 'retrying'`, keep the dashed path, add `retrying` to the flow layer, pause the dash animation, and add a pulsing ring around the destination host. If the page was just refreshed, draw the path; the ring and the progress text show the state.
  ```js
  const p = FLOW_NODES[flowDest];
  const ring = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  ring.setAttribute("class", "retry-ring");
  ring.setAttribute("cx", p.x);
  ring.setAttribute("cy", p.y);
  ring.setAttribute("r", 34);
  ```
- When `phase === 'failover'`, set `flowDest = 'h4'`, rebuild the dashed route, and let the dashes run. The first progress line switches the phase to `running`, so the bar moves.
- When the transfer resumes on h3, `phase === 'resuming'`, the route rebuilds to h3 and the dashes run again. The jump back to the h3 route is the visible recovery.
- The active server gets the blue `.host.active-server` border; on failover it moves to h4.
- On `done` / `failed` / `stopped`, clear the flow as today.

Honesty note: the path is computed from the link classes on screen, not from the controller. The real path may differ. The existing code already says this; keep the comment.

### 6.4 Progress bar and text

`updateProgress(state)` handles the new phases:

| Phase      | Text                                                         | Bar                                        |
| ---------- | ------------------------------------------------------------ | ------------------------------------------ |
| `running`  | `Transferring big.bin: 31.2%`                                | `.running`, width = progress               |
| `retrying` | `h3 unreachable, retrying 1/2 (chunk 40/128)`                | `.running .retrying`, width stays at 31.2% |
| `resuming` | `Resuming at chunk 40/128`                                   | `.running .resuming`, width stays          |
| `failover` | `Restarting on backup server h4 — sent/total`; the first chunk switches to `Transferring ...` | `.running .failover`, width follows progress |
| `done`     | `Idle — last transfer ... completed ...`                     | reset to 0, as today                       |
| `failed`   | `Idle — last transfer ... failed (server h3 did not return)` | reset                                      |
| `stopped`  | `Idle — transfer ... stopped by user`                        | reset                                      |

The progress bar must not drop to 0 during `retrying` or `resuming`. That would look like the backup restarted when it didn't. On failover it does start from 0, but the first `[PROGRESS]` line switches the phase to `running`, so the bar moves as chunks arrive.

### 6.5 Event log

Add markers and classes:

| Marker                         | Class          | Color                   |
| ------------------------------ | -------------- | ----------------------- |
| `[SERVER]`                     | `evt-server`   | Red                     |
| `[RETRY]`                      | `evt-retry`    | Amber                   |
| `[RESUME]`                     | `evt-resume`   | Green                   |
| `[FAILOVER]`                   | `evt-failover` | Purple                  |
| `[RECOVERY]`                   | `evt-recovery` | Existing green headline |
| `[DONE]` / `[FAIL]` / `[LINK]` | Existing       | Existing                |

Lines the bridge writes:

```
[SERVER] h3 down (confirmed by Mininet)
[RETRY] Polling h3 (1/2)
[RETRY] Polling h3 (2/2)
[LINK] h3 process state says alive, but its path is unreachable by TCP; waiting for the network to recover
[RESUME] h3 back; continuing at chunk 40/128
[RECOVERY] Server h3 back after 8.4s; resumed at chunk 40
[FAILOVER] h3 stayed unreachable after path wait; restarting on h4 from chunk 0
[DONE] Transfer done: big.bin — 128/128 chunks in 41.2s at ...
```

### 6.6 Accessibility

- Add `aria-live="polite"` to `#progress-text` and a visually hidden `#server-status-text` that says "Server h3 down" / "Server h3 back".
- The blue/grey/green host states must not be the only signal. The event log and the progress text carry the same information.
- `@media (prefers-reduced-motion: reduce)` disables the host pulse, the green flash, and the barber-pole animation. The state text remains.
- Buttons keep visible focus outlines.

### 6.7 CSS

Add to `style.css`:

```css
.host.active-server {
  stroke: #4ea8de;
  stroke-width: 4;
  filter: drop-shadow(0 0 6px rgba(78, 168, 222, 0.85));
}
.host.down {
  stroke: #555;
  fill: #111;
  opacity: 0.45;
  stroke-dasharray: 2, 3;
}
.host.recovering {
  stroke: #7bf1c3;
  animation: host-flash-green 0.6s ease-in-out 2;
}
.host.unknown {
  stroke: #666;
  opacity: 0.5;
}
.flow-line {
  fill: none;
  stroke: #4ecca3;
  stroke-width: 3;
  stroke-linecap: round;
  stroke-dasharray: 2 8;
  animation: flow-dash 0.8s linear infinite;
}
.flow-layer.retrying .flow-line {
  stroke: #f0a500;
  opacity: 0.45;
  animation-play-state: paused;
}
.flow-layer.retrying .link.active {
  stroke: #f0a500;
  stroke-width: 2;
  stroke-dasharray: 4, 4;
}
.retry-ring {
  fill: none;
  stroke: #f0a500;
  stroke-width: 2;
  stroke-dasharray: 4, 4;
  animation: retry-spin 1.2s linear infinite;
}
#progress-bar.retrying {
  background-image: repeating-linear-gradient(
    45deg,
    #f0a500,
    #f0a500 10px,
    #b36b00 10px,
    #b36b00 20px
  );
  animation: barberpole 0.8s linear infinite;
}
#progress-bar.failover {
  background: #e94560;
}
.event-entry.evt-server {
  color: #e94560;
  border-left: 3px solid #e94560;
  padding-left: 0.4rem;
}
.event-entry.evt-retry {
  color: #f0a500;
  border-left: 3px solid #f0a500;
  padding-left: 0.4rem;
}
.event-entry.evt-resume {
  color: #7bf1c3;
  border-left: 3px solid #4ecca3;
  padding-left: 0.4rem;
}
.event-entry.evt-failover {
  color: #c792ea;
  border-left: 3px solid #7b4fbf;
  padding-left: 0.4rem;
}

@keyframes host-flash-green {
  0%,
  100% {
    stroke: #7bf1c3;
  }
  50% {
    stroke: #4ecca3;
  }
}
@keyframes flow-dash {
  to {
    stroke-dashoffset: -10;
  }
}
@keyframes retry-spin {
  to {
    stroke-dashoffset: -16;
  }
}
@media (prefers-reduced-motion: reduce) {
  .host.down,
  .host.recovering,
  .retry-ring,
  .flow-line,
  #progress-bar.retrying {
    animation: none;
  }
}
```

---

## 7. Sequence diagrams

### 7.1 Same-server retry

```
Browser          Flask            bridge(h1)          client            h3
  | Kill h3        |                  |                  |               |
  |--------------->| server_request --|------------------|-------------->| terminate
  |                |                  |                  |               | socket closes
  |                |                  |<-- [RESUME_POINT] 40 ------------|
  |                |                  | phase=retrying   |               |
  |                |                  | probe h3:9000 twice, 5s apart    |
  |                |                  | check fresh server_status.json: |
  |                |                  | h3 process alive but TCP down   |
  |                |                  | wait for path recovery          |
  | Restart h3     |                  |                  |               |
  |--------------->| server_request --|------------------|-------------->| start
  |                |                  | probe ok         |               |
  |                |                  | spawn client --resume 40         |
  |                |                  |                  | HELLO resume  |
  |                |                  |                  |-------------->| ACK(40)
  |                |                  |                  | DATA 40..127  |
  |                |                  |<-- [COMPLETE] ---|               |
  |                |                  | result done, attempts=2, resumed_from=40
```

### 7.2 Failover to h4

```
bridge(h1)                         client                    h4
  | probe h3 twice, 5s apart (fails) but process is alive |              |
  | path wait up to 60s (still fails)                |                        |
  | phase=failover                                   |                        |
  | spawn client --servers h4 --resume 0                      |
  |                                  | HELLO resume_from=0    |
  |                                  |----------------------->| ACK(0)
  |                                  | DATA 0..127            |
  |                                  |<-----------------------| ACK(i)
  |<-- [COMPLETE] -------------------|                        |
  | result done, attempts=2, restarted_on_backup=true         |
```

### 7.3 Control path

```
Browser POST /api/server-down
  -> Flask writes server_request.json
  -> topology watcher reads it, calls proc.terminate(), waits, writes server_result.json
  -> Flask reads the result, logs [SERVER] h3 down, emits server_event
  -> UI calls updateHostStatus('h3', 'down')
```

---

## 8. Tests

### 8.1 Backend and protocol

| Test                                                 | Asserts                                                                                      |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `test_lost_ack_retry_overwrites_same_chunk`          | Server writes chunk 3, drops the ACK; retry writes chunk 3 again; file matches, no duplicate |
| `test_duplicate_chunk_is_idempotent`                 | Sending chunk 5 twice produces one copy and two ACKs                                         |
| `test_out_of_order_chunk_lands_at_its_index`         | Chunks 2 then 1 write at the right offsets                                                   |
| `test_resume_with_missing_file_restarts_at_zero`     | HELLO resume 5 with no file; server ACKs 0; client starts at 0                               |
| `test_resume_with_partial_file_acks_requested_start` | Partial file at chunk 5; server ACKs 5                                                       |
| `test_client_emits_resume_point_on_connection_loss`  | `[RESUME_POINT]` has `next_chunk = last_ack + 1`                                             |
| `test_client_honors_server_accepted_start`           | Server ACKs 0 for a requested resume; client restarts at 0                                   |
| `test_empty_file_completes`                          | 0-byte file gets ACK, `[STORED]`, `[COMPLETE]`                                               |

### 8.2 Bridge

| Test                                      | Asserts                                                                    |
| ----------------------------------------- | -------------------------------------------------------------------------- |
| `test_retry_same_server_with_resume`      | First attempt fails at chunk 3; second call gets `--resume 3`; result done |
| `test_failover_to_h4_starts_at_zero`      | h3 never returns; second call gets `--servers h4 --resume 0`               |
| `test_retry_wait_respects_cancel`         | Cancel during the two-probe wait stops the loop, no new client             |
| `test_path_wait_honors_cancel`              | Cancel during path recovery stops the loop, no new client                  |
| `test_process_alive_blocks_failover_to_backup` | Live server + unreachable TCP retries h3 instead of starting h4         |
| `test_process_dead_allows_failover_to_backup`  | Confirmed-dead server starts the h4 attempt from chunk 0                |
| `test_retry_budget_expires`               | h3 stays down; failover starts after the wait, not before                  |
| `test_result_reports_attempts_and_resume` | `attempts`, `resumed_from`, `server`, `restarted_on_backup`                |
| `test_progress_keeps_chunk_during_retry`  | Progress file has `phase=retrying`, old `chunk`, `running=true`            |

### 8.3 Server control

| Test                                    | Asserts                                |
| --------------------------------------- | -------------------------------------- |
| `test_kill_terminates_process`          | `terminate` called, wait, state down   |
| `test_restart_starts_new_process`       | New Popen stored, state up             |
| `test_kill_already_down_is_idempotent`  | Returns applied true, no exception     |
| `test_restart_already_up_is_idempotent` | No second process                      |
| `test_status_file_reflects_exit`        | `alive: false` after the process exits |
| `test_invalid_host_rejected`            | `h1` rejected with a message           |

### 8.4 Flask

| Test                                      | Asserts                                                                              |
| ----------------------------------------- | ------------------------------------------------------------------------------------ |
| `test_server_down_writes_request`         | `server_request.json` has host, action, request_id                                   |
| `test_server_down_confirmed`              | 200 with applied true; event logged                                                  |
| `test_server_down_unconfirmed_504`        | No result file; 504 with the topology hint                                           |
| `test_server_status_in_payload`           | `/api/status` includes `servers.h3.alive`                                            |
| `test_retry_phase_in_status`              | `/api/status` copies `phase`, `attempt`, `resume_from`                               |
| `test_server_recovery_clock`              | First chunk advance after a down stamps `server_recovery_time` and logs `[RECOVERY]` |
| `test_server_recovery_not_double_counted` | One measurement per run                                                              |

### 8.5 Browser (Playwright, optional but recommended)

| Test                                 | Asserts                                                      |
| ------------------------------------ | ------------------------------------------------------------ |
| `test_kill_server_marks_host_down`   | `#host-h3` has `.down`; event log has `[SERVER]`             |
| `test_retry_text_and_bar`            | `#progress-text` says retrying; bar width doesn't drop to 0  |
| `test_restart_flashes_recovering`    | `.recovering` appears, then clears                           |
| `test_failover_switches_flow_to_h4`  | `flowDest` becomes h4; h4 gets `.recovering`                 |
| `test_reduced_motion_disables_pulse` | With `prefers-reduced-motion`, `.host.down` has no animation |

### 8.6 Manual demo

1. Put a 50–100 MB file in `shared_files/`.
2. Start the transfer. Wait for about 30%.
3. Press **Kill Server h3**. The host dims to grey, the route stays visible, the bar holds, the log says retrying.
4. Wait 3-5 s, press **Restart h3**. The host flashes green, the bar continues from the same chunk, the log says `[RESUME]`.
5. When it finishes, run `sha256sum shared_files/<file> stored_files/h3/<file>`. They match.
6. Repeat, but leave h3 down. After the two probes, 5 s apart, the run restarts on h4, h4 gets the blue border, the bar moves, and the file completes. The log says `[FAILOVER]`.

---

## 9. Acceptance criteria

- [ ] Killing h3 at any chunk and restarting it before the second probe completes the same file.
- [ ] The resumed run sends only chunks from `next_chunk` onward.
- [ ] A lost ACK never duplicates or corrupts a chunk.
- [ ] The dashboard dims h3 to grey, keeps the route visible, and shows "retrying 1/2" with a held bar.
- [ ] Restarting h3 flashes it green and continues the bar from the same percentage.
- [ ] The active destination has a blue border; it moves to h4 on failover.
- [ ] If h3 stays down, the run restarts on h4, the bar moves again, and the run says so.
- [ ] The h3 partial is hidden as `.part` and does not appear in Stored Files.
- [ ] A cancel during the retry stops the run and starts no new client.
- [ ] A page refresh during the retry shows the same phase, attempt, server, and chunk.
- [ ] A second tab sees the same state within one poll.
- [ ] The event log has `[SERVER]`, `[RETRY]`, `[RESUME]`, `[RECOVERY]`, and `[DONE]` or `[FAILOVER]`.
- [ ] Reduced motion disables the pulses; the text still explains the state.
- [ ] The existing link-failure demo and its tests still pass.

---

## 10. Risks and limits

- **PID namespace.** Mininet hosts share the PID namespace. Never run `pkill -f server.py` inside a host to kill one server; it can kill both. Use the stored Popen handles. If you must use `pkill`, match `--host 10.0.0.3`.
- **Port reuse.** After `terminate`, wait for the process to exit before starting a new one. `SO_REUSEADDR` is set on the server, so a short TIME_WAIT is fine.
- **Real host failure.** The button kills the process, so the socket closes and the client fails fast. A host blackhole still waits out the 60 s ACK timeout. Say that in the demo.
- **Failover restarts from zero.** h4 has no partial file. Passing `--resume N` without the accepted-start ACK would corrupt it; the ACK change is what makes this safe.
- **Retry budget.** The 5 s probe gap plus two client attempts must fit the demo. A 1 GB file may exceed the 300 s per-attempt deadline; the existing docs already say the big file is slow.
- **Status file staleness.** If `topology.py` dies, `server_status.json` goes stale. Treat older than 10 s as unknown, not down.
- **Animation is illustrative.** The flow path is computed from the link classes on screen, not from the controller. The real path may be via s3 or s4. Keep the existing honesty comment.
- **Partial files.** In-progress copies are `<filename>.part` and `/api/files` hides them. Stale `.part` files from abandoned runs stay on disk until cleaned; they are not listed as backups. Temp plus rename can nearly double the space during a transfer.

---

## 11. Implementation order

| Step | Work                                                               | Files                                               |
| ---- | ------------------------------------------------------------------ | --------------------------------------------------- |
| 1    | Server writes by index, tracks `received`, ACKs the accepted start | `server/server.py`                                  |
| 2    | Client emits `[RESUME_POINT]`, honours the accepted start          | `client/client.py`                                  |
| 3    | Bridge retry/failover state machine, progress and result fields    | `mininet/bridge.py`                                 |
| 4    | Server control watcher, process handles, status file               | `mininet/serverctl.py` (new), `mininet/topology.py` |
| 5    | Flask routes, status payload, recovery clock, events               | `frontend/app.py`                                   |
| 6    | Buttons, host classes, phase text, flow destination, CSS, a11y     | `dashboard.html`, `dashboard.js`, `style.css`       |
| 7    | Tests and docs                                                     | `tests/`, `docs/`                                   |

Steps 1 and 2 are inseparable: the accepted-start ACK is what makes step 3 safe. Step 4 can run in parallel with steps 1-3 because it only touches Mininet process control.

---

## 12. Open questions

- Kill h3 only, or make both servers selectable? v1 fixes h3. - selectable dropdown within the button itself.
- Manual Restart button, or auto-restart after N seconds? Manual matches the link down/up pattern. - Manual
- 30 s wait, or failover immediately after one retry? 30 s gives the operator time to restart. - once we click button logs must show polling for h3 2 times , 5 seconds apart then the actual failur must be confirmed.
- Should the Metrics panel show a separate "Server recovery" row, or reuse "Recovery Time" with a kind label? Reuse with a label is less UI work. - reuse "Recovery Time" with a lable, make sure to lable the previous cases appropriatly.
- Do you want the client's connected server shown in the Metrics panel? The `server` field makes it easy. - show it in the topology like if transfer takes place to h3 highlight the border if it fials, darken it and highlight h4 with appropriate route highlight from before too.

---

## 13. Note

- before editing any component of the frontend or backend understand all strings attachted to it.
- for example if the topology has animations or dynamic stuff understand how it works and when it works and implmenet it for future features.
