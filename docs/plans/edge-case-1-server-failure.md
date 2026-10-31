# Implementation Plan: Edge Case 1 — Server Failure Recovery

**Branch:** `edge-case-1`
**Source spec:** `docs/specs/server-failure-recovery.md`
**Status:** Ready to build
**Date:** 2026-10-31

---

## Overview

Implement the server-death recovery path from the spec. A transfer that loses h3 either resumes on h3 after a manual restart or restarts on h4. The dashboard gets a server target dropdown, Kill/Restart buttons, a visible server state in the topology, and recovery timing labeled by kind.

The plan follows the existing link-failure pattern: one control path, one state file, one event stream, and one topology state.

---

## Decisions from the answered spec questions

| # | Question | Decision |
|---|---|---|
| 1 | One server or both? | Selectable h3/h4 via a dropdown in the server control group. |
| 2 | Manual or automatic restart? | Manual. No timer, no auto-restart. |
| 3 | Retry wait? | Two probes, 5 seconds apart, then confirm. This replaces the spec's 30 s wait. |
| 4 | Metrics panel? | Reuse "Recovery Time" with a kind label. Label link cases and server cases separately. |
| 5 | Show the active server? | Yes, in the topology: blue border on the active server, grey disabled state on failure, h4 and the route highlighted on failover. |
| 6 | Partial-file policy? | Write `<filename>.part`, promote only on completion, and hide `.part` from `/api/files`. |
| 7 | Topology layout? | Center the drawing with the SVG viewBox; keep the route visible while a server is down. |

### Assumptions to confirm before coding

- **Probe timing.** "2 times, 5 seconds apart" means probe at t=0, probe at t=5, confirm after the second probe. If you meant 5 s after each probe (10 s total), change `SERVER_PROBE_INTERVAL` from 5 to 5 and add a second wait; the code shape stays the same.
- **Dropdown.** A native `<select id="server-target">` in the same button group as Kill and Restart. It is accessible and small. A true split button with a custom menu is a later polish item.
- **Same probe policy in the bridge.** After the client fails, the bridge uses the same two probes at 0 s and 5 s. If h3 answers, it retries with resume. If both fail, it fails over to h4.
- **Active server source.** The bridge's progress carries `server: "h3" | "h4"`. The topology highlight follows that field, not a guess.
- **No cross-server resume.** Failover to h4 starts at chunk 0. The server's ready ACK carries the accepted start so the client can't accidentally write at the wrong offset.

---

## Dependency graph

```
Task 1  server indexed writes + accepted start
   │
   └── Task 2  client resume point + accepted start
          │
          └── Task 3  bridge retry / failover
                 │
                 └── Task 5  Flask API + recovery state
                        │
                        ├── Task 6  server dropdown + buttons
                        ├── Task 7  topology states + route
                        └── Task 8  progress / log / labels / a11y

Task 4  serverctl + topology handles   (parallel with 1-3)
   │
   └── Task 5  Flask server status

Task 9  tests, docs, demo   (after all)
```

Tasks 1 and 2 are inseparable. Task 3 is unsafe without them.

---

## Phase 0: Pre-flight

### Task 0: Inventory the touched surface

**Description.** Read the spec and every file it touches before editing. List the element IDs, CSS classes, API routes, side-channel files, event markers, user-facing strings, and the animation call chain. The appendix below is the starting inventory; confirm it against the current files and add anything missing.

**Acceptance criteria.**

- [ ] Every element ID in `dashboard.html` is listed, including the new ones.
- [ ] Every CSS class that changes state is listed.
- [ ] Every API route and its method is listed.
- [ ] Every side-channel file has a producer and a consumer.
- [ ] Every event marker and its CSS class is listed.
- [ ] Every new or changed user-facing string has a source file and an owner.
- [ ] The animation chain is written down, including where progress drives the dots.
- [ ] No code changes in this task.

**Verification.** Compare the appendix with `dashboard.html`, `dashboard.js`, `style.css`, `frontend/app.py`, `mininet/bridge.py`, and `mininet/serverctl.py` (when it exists). Fix the appendix before starting Task 1.

**Files.** Read-only: `frontend/`, `mininet/`, `client/`, `server/`, `docs/specs/server-failure-recovery.md`.

**Size.** S.

---

## Phase 1: Protocol foundation

### Task 1: Server writes chunks by index

**Description.** Change the server from a sequential file position to an index-based write. Track received chunk indices in a set. Accept a resume only when the `.part` file is long enough. Send the accepted start in the ready ACK. Write to `<filename>.part` and promote it only when the file is complete. Handle the empty file.

**Acceptance criteria.**

- [ ] `_handle_data` seeks to `frame['index'] * CHUNK_SIZE`, writes, flushes, and adds the index to `received`.
- [ ] A duplicate index ACKs again without writing a second copy.
- [ ] An out-of-order index writes at its own offset.
- [ ] `_handle_hello` sends `ACK(accepted_start)`: the requested resume when the `.part` file is long enough, otherwise 0.
- [ ] A 0-byte file is promoted and `[STORED]` prints before the ready ACK.
- [ ] The final chunk promotes `.part` with `os.replace` before the final ACK.
- [ ] `[STORED]` prints once, when every index is present.
- [ ] An index outside `0..total-1` is dropped.
- [ ] `/api/files` hides `.part`, so an interrupted copy is not listed as a backup.

**Verification.**

```bash
python -m pytest tests/test_transfer.py tests/test_recovery.py -q
```

**Dependencies.** Task 0.

**Files.**

- `server/server.py`
- `tests/test_transfer.py`
- `tests/test_recovery.py`

**Size.** M.

---

### Task 2: Client resume point and accepted start

**Description.** Print a structured resume point on any chunk failure. Read the ready ACK payload and use it as the start chunk. If the server restarts from 0, say so.

**Acceptance criteria.**

- [ ] On a lost connection or missing ACK, the client prints one line:
      `[RESUME_POINT] {"last_ack":39,"next_chunk":40,"total":128,"server":"10.0.0.3:9000"}`
- [ ] `next_chunk` is `last_ack + 1`.
- [ ] The ready ACK payload overrides `--resume N` when the server accepts a lower start.
- [ ] A server that accepts the requested resume keeps the requested start.
- [ ] A failed transfer still exits 1.
- [ ] A successful transfer still prints `[COMPLETE]` and exits 0.

**Verification.**

```bash
python -m pytest tests/test_client_resume.py tests/test_recovery.py -q
```

**Dependencies.** Task 1.

**Files.**

- `client/client.py`
- `tests/test_client_resume.py` (new)

**Size.** S.

---

### Checkpoint 1: Protocol

- [ ] `pytest tests/test_transfer.py tests/test_server_resume.py tests/test_client_resume.py tests/test_recovery.py -q` passes.
- [ ] No bridge or UI changes yet.
- [ ] A manual two-process transfer on localhost still completes.

---

## Phase 2: Bridge retry and failover

### Task 3: Bridge retry / failover state machine

**Description.** `run_client` becomes one attempt with a result dict. `process_request` owns the retry loop. After a failure with a resume point, the bridge probes the same server twice, 5 s apart. If it answers, retry with `--resume next_chunk`. If both probes fail, fail over to h4 with `--resume 0`.

**Acceptance criteria.**

- [ ] `run_client` returns `status`, `error`, `resume_point`, `server`, `chunks`.
- [ ] The bridge parses `[RESUME_POINT]` and `[CLIENT] Connected to`.
- [ ] On the first failure, progress shows `phase: "retrying"`, `attempt: 1`, `resume_from: N`, `server: "h3"`, `running: true`.
- [ ] Probe 1 logs at t=0; probe 2 logs at t=5; a failed second probe confirms the server is down.
- [ ] A probe that succeeds cancels the failover and retries the same server with `--resume N`.
- [ ] Two failed probes start the h4 attempt with `--resume 0` and `phase: "failover"`.
- [ ] Stop cancels the wait within 0.5 s; no new client starts.
- [ ] The progress file keeps the last `chunk` and `total` during the wait.
- [ ] The result carries `attempts`, `resumed_from`, `server`, `restarted_on_backup`, `downtime_s`.
- [ ] A failure without a resume point goes straight to `failed`.

**Verification.**

```bash
python -m pytest tests/test_bridge_retry.py -q
```

**Dependencies.** Tasks 1 and 2.

**Files.**

- `mininet/bridge.py`
- `tests/test_bridge_retry.py` (new)

**Size.** M.

---

### Checkpoint 2: Bridge

- [ ] Bridge tests pass with a fake server that dies after 3 chunks and restarts.
- [ ] A cancel during the wait leaves no client process and no result claiming done.
- [ ] The progress file never drops `running` to false during the wait.

---

## Phase 3: Server control

### Task 4: Server control inside Mininet

**Description.** Add `mininet/serverctl.py` with kill, restart, status, and request handling for h3 and h4. Store the `Popen` handles from `node.popen()`. Do not use `pkill -f server.py`; Mininet hosts share the PID namespace, so that can kill both servers.

**Acceptance criteria.**

- [ ] `start_server(net, host)` starts `/tmp/src/server/server.py` with the right `--host`, `--port`, and `--storage`, appends to `/tmp/server_<host>.log`, and returns the Popen.
- [ ] `apply_server_state(net, procs, host, action)` validates `h3`/`h4` and `down`/`up`.
- [ ] `down` terminates the process, waits up to 3 s, kills if needed, probes twice 5 s apart, and returns `confirmed` only after both probes fail.
- [ ] `up` is idempotent and starts a new process only when none is alive.
- [ ] `server_status.json` is written every tick with `checked_at` and per-host `alive`, `pid`, `since`.
- [ ] The topology stores the handles, starts the watcher thread, clears stale control files on boot, and terminates the servers on stop.
- [ ] An unexpected exit shows `alive: false` without waiting for a button.

**Verification.**

```bash
python -m pytest tests/test_serverctl.py -q
```

**Dependencies.** Task 0. Can run in parallel with Tasks 1-3.

**Files.**

- `mininet/serverctl.py` (new)
- `mininet/topology.py`
- `tests/test_serverctl.py` (new)

**Size.** M.

---

### Checkpoint 3: Server control

- [ ] Fake-net tests kill and restart both h3 and h4.
- [ ] The status file updates when a process exits without a request.
- [ ] No code path calls `pkill` by bare name.

---

## Phase 4: Flask API and state

### Task 5: Server routes, status, and recovery clock

**Description.** Add the server control routes and fold the new progress and status fields into the dashboard state.

**Acceptance criteria.**

- [ ] `POST /api/server-down` and `/api/server-up` accept `{host}` and default to h3.
- [ ] Invalid hosts return 400.
- [ ] A confirmed request returns 200 with `applied: true`.
- [ ] A rejected request returns 502; silence returns 504 with the topology hint.
- [ ] `/api/status` includes `servers` and the transfer's `phase`, `attempt`, `max_attempts`, `resume_from`, `server`, `retry_wait_s`.
- [ ] `/api/metrics` returns `recovery_time`, `recovery_kind`, `recovery_target`, `throughput`, `progress`.
- [ ] A confirmed server-down during a run starts the clock.
- [ ] The first chunk advance after the 3 s straggler grace stamps the server recovery time and logs `[RECOVERY] Server h3 back after X.Xs; resumed at chunk N`.
- [ ] Link recovery still logs and labels itself as a link.
- [ ] A new transfer resets both clocks.

**Verification.**

```bash
python -m pytest tests/test_dashboard_server.py tests/test_dashboard_busy.py -q
```

**Dependencies.** Tasks 3 and 4.

**Files.**

- `frontend/app.py`
- `tests/test_dashboard_server.py` (new)

**Size.** M.

---

### Checkpoint 4: Flask

- [ ] Server routes return 200, 400, 502, and 504 in tests.
- [ ] A stale status file reads as unknown, not down.
- [ ] Link and server recovery labels don't overwrite each other.

---

## Phase 5: Frontend

### Task 6: Server dropdown and buttons

**Description.** Add the target select and the Kill/Restart buttons. Wire them to the new routes. Keep the existing link buttons unchanged.

**Acceptance criteria.**

- [ ] A native `<select id="server-target">` offers h3 and h4; default h3.
- [ ] `#btn-server-down` posts the selected host to `/api/server-down`.
- [ ] `#btn-server-up` posts the selected host to `/api/server-up`.
- [ ] Applied, rejected, and unconfirmed responses each add a clear event line.
- [ ] Buttons are keyboard reachable and have visible focus.
- [ ] A second tab sees the state on its next poll.
- [ ] `prefers-reduced-motion` does not affect the buttons.

**Verification.**

```bash
node --check frontend/static/dashboard.js
```

Manual: click each button with h3 and h4 selected; confirm the event text and the API response.

**Dependencies.** Task 5.

**Files.**

- `frontend/templates/dashboard.html`
- `frontend/static/dashboard.js`

**Size.** S.

---

### Task 7: Topology states and route highlight

**Description.** Show the active server, the down server, and the failover route in the SVG. Reuse the existing flow animation and `.link.active` path highlight.

**Acceptance criteria.**

- [ ] The active server gets `.host.active-server` with a blue border.
- [ ] A down server gets `.host.down`, loses `.active-server`, and turns grey/dim with no pulse.
- [ ] The route to a down server stays visible, dimmed and dashed.
- [ ] On failover, h4 gets the blue active border and the h4 route.
- [ ] A restarted server gets `.host.recovering` for 1.5 s, then returns to normal.
- [ ] Unknown status gets `.host.unknown`, not `.down`.
- [ ] `flowDest` follows `state.server`; failover switches the dots to h4.
- [ ] The retry phase keeps the last dots and adds a retry ring at the destination.
- [ ] The failover phase clears the dots and highlights the h4 route.
- [ ] The resume phase recomputes the h3 route and continues at the current fraction.
- [ ] The existing link-failure animation still works.
- [ ] Reduced motion disables the pulse, flash, and ring spin.

**Verification.** Manual in a browser with a transfer running; kill and restart h3; then kill and leave it down to see the h4 route. Optional Playwright tests.

**Dependencies.** Tasks 5 and 6.

**Files.**

- `frontend/static/dashboard.js`
- `frontend/static/style.css`

**Size.** M.

---

### Task 8: Progress text, event classes, metrics label, accessibility

**Description.** Make the transfer state readable without watching the topology. Label recovery by kind and target. Add the event colors and the screen-reader line.

**Acceptance criteria.**

- [ ] `phase: "retrying"` shows `h3 unreachable, retrying 1/2 (chunk 40/128)` and holds the bar width.
- [ ] `phase: "resuming"` shows `Resuming at chunk 40/128` and holds the bar width.
- [ ] `phase: "failover"` shows `Restarting on backup server h4` and resets the bar to 0.
- [ ] `phase: "done"` and `"failed"` keep the existing terminal text and reset the bar.
- [ ] `[SERVER]`, `[RETRY]`, `[RESUME]`, `[FAILOVER]` each get a distinct event class and color.
- [ ] The metrics row reads `8.4s (server h3)` or `1.3s (link s1-s2)`.
- [ ] `#progress-text` has `aria-live="polite"`.
- [ ] A visually hidden `#server-status-text` says "Server h3 down" / "Server h3 back".
- [ ] Color is not the only cue; the text carries the same state.
- [ ] Reduced motion disables the barber-pole and pulses.

**Verification.** Manual browser check; optional Playwright for text and classes.

**Dependencies.** Tasks 5 and 7.

**Files.**

- `frontend/static/dashboard.js`
- `frontend/static/style.css`
- `frontend/templates/dashboard.html`

**Size.** M.

---

### Checkpoint 5: Frontend

- [ ] `node --check` passes.
- [ ] Kill h3 during a transfer: h3 red, bar holds, retry text, retry ring.
- [ ] Restart h3: h3 green flash, bar continues, event log `[RESUME]`.
- [ ] Leave h3 down: h4 active with the blue border, route highlight, bar moves after the first chunk, `[FAILOVER]`.
- [ ] Refresh mid-retry: state rehydrates.
- [ ] Second tab: same state within one poll.
- [ ] Reduced motion: no pulses, text still explains.

---

## Phase 6: Verification and docs

### Task 9: Tests, docs, demo

**Description.** Add the remaining tests, update the docs to the final decisions, and run the two demos.

**Acceptance criteria.**

- [ ] `python -m pytest tests -q` passes with the existing 72 tests and the new ones.
- [ ] `node --check frontend/static/dashboard.js` passes.
- [ ] The spec's 30 s wait is replaced by the two-probe 5 s policy.
- [ ] `docs/test-cases.md` lists the new cases.
- [ ] `docs/edge-cases.md` case 1 links to the spec and the plan.
- [ ] The manual demo completes with matching `sha256sum` values.
- [ ] The failover demo completes on h4 and says "restarted".
- [ ] No stale strings remain: "30 s", "v1 fixes h3", or "Metrics panel" where the decision changed.
- [ ] Optional Playwright tests run if the dependency is installed; otherwise the manual checklist is the evidence.

**Verification.**

```bash
python -m pytest tests -q
node --check frontend/static/dashboard.js
git status --short
```

**Dependencies.** Tasks 1-8.

**Files.**

- `tests/test_dashboard_ui.py` (new, optional)
- `docs/specs/server-failure-recovery.md`
- `docs/edge-cases.md`
- `docs/test-cases.md`
- `docs/runbook/MASTER.md` (only if commands change)

**Size.** M.

---

## Commit sequence

| # | Commit message | Tasks |
|---|---|---|
| 1 | `feat(server): write chunks by index and accept resume offsets` | 1 |
| 2 | `feat(client): report resume point and honour accepted start` | 2 |
| 3 | `feat(bridge): retry or fail over after server failure` | 3 |
| 4 | `feat(mininet): add server kill and restart control` | 4 |
| 5 | `feat(dashboard): add server failure API and state` | 5 |
| 6 | `feat(dashboard): add server controls and topology states` | 6, 7 |
| 7 | `feat(dashboard): label server recovery and retry phases` | 8 |
| 8 | `test: cover server failure recovery` | 9 tests |
| 9 | `docs: update server failure recovery docs` | 9 docs |

Each commit should leave the suite green. Do not squash the protocol commit into the bridge commit; the protocol change is the foundation.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Retry duplicates a chunk because the server still writes at `_file_pos` | Data corruption | Task 1 before Task 3. Test the lost-ACK case. |
| `pkill -f server.py` kills both servers | Demo breaks | Use stored Popen handles. Only match `--host 10.0.0.3` if pkill is unavoidable. |
| 5 s probe window is shorter than a manual restart | The run fails over to h4 instead of resuming | Click Restart within 5 s. If that is too tight, change one constant; the code shape stays. |
| Dropdown sends an invalid or stale host | 400 or wrong server killed | Validate in Flask and serverctl; default to h3; read the select at click time. |
| Active-server class and down class fight | Wrong border | Define CSS order: `.host.down` wins over `.host.active-server`; remove active on down. |
| Status file is stale after topology dies | UI shows a false state | Age > 10 s means unknown, not down. |
| Metrics label confuses link and server recovery | Wrong number in the viva | Always show `(link <pair>)` or `(server <host>)`; test both. |
| `.part` temp file from edge case 3 | Resume targets the wrong file | Decide before Task 1: either land case 3 first or keep the final filename for this branch. |
| Playwright is not installed | No browser test evidence | Manual checklist is acceptable; keep the Playwright tests optional. |

---

## Definition of done

- [ ] Kill h3 at any chunk, restart within 5 s, and the same file completes.
- [ ] Only chunks from `next_chunk` onward are sent after the resume.
- [ ] A lost ACK never duplicates or corrupts a chunk.
- [ ] The dashboard shows the retry, the resume, and the failover with the right colors and text.
- [ ] The active server is visible in the topology; h4 takes the highlight on failover.
- [ ] Recovery Time shows the kind and target.
- [ ] Cancel during the wait stops the run.
- [ ] Refresh and a second tab show the same state.
- [ ] Reduced motion and keyboard access work.
- [ ] The link-failure demo and its tests still pass.
- [ ] The spec, edge-cases report, test cases, and runbook agree with the final behavior.

---

## Appendix: Pre-flight inventory

### Element IDs

Existing: `topology-svg`, `sw-s1`…`sw-s4`, `host-h1`…`host-h4`, `link-h1-s1`, `link-h2-s3`, `link-s2-h3`, `link-s4-h4`, `link-s1-s2`, `link-s3-s4`, `link-s1-s3`, `link-s2-s4`, `link-s1-s4`, `link-s2-s3`, `btn-start`, `btn-stop`, `btn-link-down`, `btn-link-up`, `btn-refresh-files`, `btn-refresh-browse`, `progress-container`, `progress-bar`, `progress-text`, `browse-list`, `throughput-value`, `recovery-value`, `chunks-value`, `bridge-value`, `throughput-chart`, `files-list`, `event-log`.

New: `server-target`, `btn-server-down`, `btn-server-up`, `server-status-text`.

### CSS classes

Existing: `.host`, `.host.client`, `.host.server`, `.link`, `.link.down`, `.link.active`, `.flow-dot`, `.flow-dot.trail`, `#progress-bar.running`, `#progress-bar.done`, `#progress-bar.failed`, `#progress-bar.stopped`, `.event-entry.evt-recovery`, `.event-entry.evt-link`, `.event-entry.evt-done`, `.event-entry.evt-fail`, `barberpole`.

New: `.host.active-server`, `.host.down`, `.host.recovering`, `.host.unknown`, `.retry-ring`, `.flow-layer.retrying`, `#progress-bar.retrying`, `#progress-bar.failover`, `.evt-server`, `.evt-retry`, `.evt-resume`, `.evt-failover`.

### JavaScript functions and animation chain

`updateProgress` renders the bar, text, metrics, and calls `renderFlow`. `renderFlow` uses `computeFlowPath`, `flowLayer`, `pointAlongPath`, `highlightFlowPath`, and `clearFlow`. `computeFlowPath` does BFS over `FLOW_MESH` from `FLOW_SRC` to `FLOW_DST`, skipping links with `.down`. `updateLinkStatus` toggles `.down` on a link. `flowDots` and `flowPathKey` cache the current route.

New: `updateHostStatus`, `updateActiveServer`, `flowDest`, `renderRetryRing`. The route is not blocked by a down host.

### API routes

Existing: `/`, `/api/files`, `/api/browse`, `/api/transfer-result`, `/api/start-transfer`, `/api/stop-transfer`, `/api/link-down`, `/api/link-up`, `/api/bridge`, `/api/status`, `/api/metrics`.

New: `/api/server-down`, `/api/server-up`.

### Side-channel files

Existing: `transfer_request.json`, `transfer_result.json`, `transfer_progress.json`, `transfer_events.json`, `transfer_cancel.json`, `link_request.json`, `link_result.json`, `bridge.lock`, `bridge_alive.json`.

New: `server_request.json`, `server_result.json`, `server_status.json`.

### Event markers

Existing: `[CLIENT]`, `[PROGRESS]`, `[COMPLETE]`, `[STORED]`, `[LINK]`, `[RECOVERY]`, `[DONE]`, `[FAIL]`.

New: `[SERVER]`, `[RETRY]`, `[RESUME]`, `[FAILOVER]`, `[SERVERCTL]`.

### User-facing strings to add or change

- Buttons: `Target`, `Kill Server`, `Restart Server`.
- Progress: `h3 unreachable, retrying 1/2 (chunk N/M)`, `Resuming at chunk N/M`, `Restarting on backup server h4`.
- Events: `[SERVER] h3 down (confirmed by Mininet)`, `[RETRY] Polling h3 (1/2)`, `[RETRY] Polling h3 (2/2)`, `[RESUME] h3 back; continuing at chunk N/M`, `[FAILOVER] h3 down confirmed; restarting on h4 from chunk 0`, `[RECOVERY] Server h3 back after X.Xs; resumed at chunk N`.
- Metrics: `Recovery Time`, value suffixed with `(server h3)` or `(link s1-s2)`.
- Status: `Server h3 down`, `Server h3 back`, `Server h3 unknown`.
- API errors: `Invalid server: <host>`, `Mininet did not confirm within 5s — is mininet/topology.py running?`.
