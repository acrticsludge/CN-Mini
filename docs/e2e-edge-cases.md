# E2E Edge-Case Register — Distributed Backup System

Deep scan of every failure mode across the full path:

```
Dashboard (Flask) -> transfer_request.json -> Bridge (h1) -> TCP Client -> TCP Server -> stored_files/h3|h4
                                                ^             ^
                                            retry/failover   SDN-controlled path
```

Each case lists: scenario, risk, current state, and where it is handled (file:line).

## Phase 1 — Request submission (dashboard → request file)

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E1 | Double-click / two Start POSTs at once | Duplicate transfer, interleaved progress, "stored but UI says failed" | Fixed | `frontend/app.py:71` `_start_lock` + `frontend/app.py:141` `_bridge_busy()` → second click gets 409. Tested by `tests/test_e2e_local.py`. |
| E2 | Start for a file not in shared_files/ | 500 or silent failure | Fixed | `frontend/app.py:541` → 404 "File not found". |
| E3 | Filename ends in `.part` | Collides with the receiver's own temp path for the same base name | Fixed | `frontend/app.py:535` rejects `.part` sources; `server/server.py:136` rejects `.part` at HELLO (`code=invalid`). |
| E4 | Trailing dots/spaces in filename (Windows aliasing) | `x`, `x.part.`, `x.part ` resolve to one path, two claims alias one temp file | Fixed | `server/server.py:143` rejects names where `rstrip(' .')` changes the name. |
| E5 | Target server full (quota) | Bytes sent to a server that cannot store them | Fixed | `frontend/app.py:561` auto-routes to the other server when target is full; bridge `_server_free_bytes()` re-checks at failover. |
| E6 | Both servers full | Silent data loss / unhelpful error | Fixed | `frontend/app.py:562` → 409 "Both servers full". |
| E7 | File size == free space exactly | Off-by-one MB rounding rejects a valid transfer | Fixed | `frontend/app.py:554` `_is_full` uses `>` (strict); `mininet/bridge.py:372` clamps free at 0. Tested `test_exact_fit_allows`. |
| E8 | Partial-MB usage (512 KB used + 768 KB need vs 1 MB quota) | Rounded MB check wrongly allows | Fixed | Byte-accurate `_used_bytes` in `frontend/app.py:235`; tested `test_partial_mb_usage_routes_over_quota`. |
| E9 | Quota 0 = unlimited | Large file wrongly rejected | Fixed | `frontend/app.py:549` `quota_mb == 0` → `None` (unlimited). |
| E10 | Request h4 when only h3 configured | Silent fallback to wrong server | Fixed | `mininet/bridge.py:478` rejects `h4` when `len(SERVERS) < 2`. |
| E11 | Quota changed while a transfer is in flight | In-flight request re-routed mid-run | Fixed | `frontend/app.py:570` routing decision is made once at queue time; `test_mid_transfer_quota_change_no_effect`. |
| E12 | Resume: only remaining bytes need space, but routing checks whole file | Conservative — may route away or 409 although a resume would fit | Known, safe | Not a data-safety bug; the check intentionally over-reserves. |
| E13 | Corrupt / half-written request file | Bridge retries a bad request forever | Fixed | `mininet/bridge.py:690` drops unparseable request files. |

## Phase 2 — Bridge picks up request → runs TCP client

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E14 | Stale bridge lock held by a dead process | New bridge exits, no bridge runs at all | Fixed | `mininet/bridge.py:90` lock reclaimed after 10 s by file freshness (not pid probe). |
| E15 | Two bridges spawned (topology restarted without cleanup) | Duplicate transfers, interleaved result writes | Fixed | `mininet/bridge.py:90` second bridge exits; `topology.py:95` kills leftovers and removes orphan lock before launch. |
| E16 | Bridge runs in host namespace instead of h1 | Every `connect()` fails "Network is unreachable" though pingall is clean | Fixed | `mininet/bridge.py:117` uses `popen()` (namespace); `mininet/bridge.py:639` `report_namespace()` warns on wrong IP. |
| E17 | Both servers unreachable before start | Hang for 60 s, no clear reason | Fixed | `client/client.py:41` connect per server with `connect_timeout`; "No backup server reachable", exit 1, no partial file. Tested `test_both_servers_unreachable_reports_clear_reason`. |
| E18 | Link killed mid-transfer (SDN blackhole) | 10 s socket timeout kills a transfer the reroute would have saved | Fixed | `client/client.py:32-33` separates `connect_timeout` (10 s) from `transfer_timeout` (60 s) to outlast STP reconvergence. Tested `tests/test_recovery.py::TestStallRecovery`. |
| E19 | Server dies mid-transfer | Half file stranded, no retry, partial truncated on next Start | Fixed | Client reports `[RESUME_POINT]` (`client/client.py:206`); bridge retries same server with `--resume` (`mininet/bridge.py:560`), then failover from chunk 0. |
| E20 | **Failover to backup server that has less space than needed** (the reported bug: kill h4 mid-way, swap to h3) | Bytes sent to h3 and fail there, or partial write then disk-full | **Fixed in this change** | `mininet/bridge.py:342` `_server_free_bytes()` now reads real disk usage in `stored_files/<host>` instead of the stale `used_mb` from `server_storage.json`. Failover is blocked when the backup cannot hold the file; run fails upfront with a clear `[STORAGE] ... also lacks space` event. Regression tests `test_bridge_failover_blocks_when_backup_disk_is_full` and `test_bridge_failover_proceeds_when_backup_disk_has_space`. |
| E21 | Failover restarts from chunk 0 on the backup | All progress on the dead server is redone | Documented | Cross-server resume is not supported; bridge labels the run `restarted_on_backup`. |
| E22 | User clicks Stop mid-transfer | Client keeps running, "stopped" ignored | Fixed | `frontend/app.py:634` writes `transfer_cancel.json`; bridge checks it each line (`mininet/bridge.py:287`) and `SIGTERM`s the client → status `stopped`. |
| E23 | Stop clicked after client already finished | Harmless cancel file races the result | Fixed | Bridge unlinks `CANCEL_FILE` on terminal (`mininet/bridge.py:303`); cancel after completion is a no-op. |
| E24 | Bridge dies mid-run; progress file left `running=True` | Every later click rejected as "busy" forever | Fixed | `frontend/app.py:138` `STALE_PROGRESS_AGE` 600 s treats old progress as idle. |
| E25 | Client exits 0 without `[COMPLETE]` (crash/segfault) | Failed run looks successful (bridge trusts exit code) | Fixed | `mininet/bridge.py:309` requires `returncode == 0 AND completed` (`[COMPLETE]` seen) for `done`. |
| E26 | Source file edited mid-transfer | Stored file is half old / half new, every chunk CRC-valid | Fixed | `client/client.py:72` snapshots size+mtime before start, re-checks at end → `[INCONSISTENT] Source changed during backup`, exit 1. Tested `test_source_changed_during_transfer_is_rejected`. |
| E27 | Empty file | Division by zero / no chunks | Fixed | `server/server.py:219` `file_size == 0` → immediate `_finish()`, 0-byte file stored. Tested `test_empty_file_completes`. |
| E28 | Duplicate chunk (lost ACK retry) | Second copy of same chunk appended | Fixed | `server/server.py:239` ACKs repeated index without rewriting; `client/client.py:155` resumes at `last_ack + 1`. Tested `test_duplicate_chunk_is_idempotent`. |
| E29 | Out-of-order chunks | File reassembled in wrong order | Fixed | Server writes each chunk at `index * CHUNK_SIZE` (`server/server.py:247`). Tested `test_out_of_order_chunk_lands_at_its_index`. |
| E30 | Source file deleted mid-transfer | `FileNotFoundError` crashes client, no resume point | **Fixed** | `client/client.py:153` catches `OSError` on open/read, reports `[RESUME_POINT]` and returns False. Tested `test_client_source_file_deleted_mid_transfer`. |
| E31 | `resume_from` beyond total chunks | Client seeks past EOF, reports success with 0 bytes | **Fixed** | `mininet/bridge.py:489` clamps `resume_from` to `total_chunks`. Tested `test_bridge_clamps_resume_from_beyond_total_chunks`. |
| E32 | Orphaned `.part` files invisible to failover disk check | Failover proceeds to server whose disk is full of `.part` files | **Fixed** | `mininet/bridge.py:342` `_disk_used_bytes()` now counts all files including `.part`. Tested `test_bridge_counts_part_files_in_failover_disk_check`. |

## Phase 3 — Server side (receiving chunks)

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E30 | Two clients back up the same filename at once | Interleaved bytes in one `.part`, corrupted result | Fixed | Claim-by-`normcase(filename)` at HELLO under `_admission_lock`; second gets `MSG_ERROR code=busy`. Tested `test_server_rejects_second_transfer_for_same_name`. |
| E31 | Case-only filename variants (`same.bin` vs `SAME.BIN`) | Both pass the claim on case-insensitive volumes | Fixed | `server/server.py:149` `os.path.normcase` claim key. |
| E32 | Quota race: two transfers both admitted, disk can't hold both | Both admitted, one fails mid-write | Fixed | `server/server.py:174` reservation under admission lock; released on finish/disconnect. Write-time `OSError` → `MSG_ERROR` (`server/server.py:251`). Tested `test_server_reserves_quota_for_inflight_transfer`. |
| E33 | Disk full or read-only mid-transfer | Silent truncation / corrupt file | Fixed | `server/server.py:251` catches `OSError` on write/flush → `MSG_ERROR`, client fails fast with `[CLIENT] Server storage error`. |
| E34 | Re-backup of an existing file, killed mid-way | Old final copy truncated/lost | Fixed | Server writes to `<name>.part` and `os.replace()`-promotes only after the last chunk (`server/server.py:289`). Old copy intact until promotion. Tested `test_failed_rebackup_keeps_previous_file`. |
| E35 | Stale `.part` files accumulate after crashes | Disk space consumed, clutter | Gap | Documented in `docs/edge-cases.md`; not yet auto-cleaned. `.part` hidden from `/api/files` (`frontend/app.py:460`) and offered as resume candidates (`/api/partial-files`). |
| E36 | Resume where part file doesn't cover the requested offset | Client writes chunk N at offset 0, corrupting the tail | Fixed | `server/server.py:206-208` accepts resume only if `part size >= resume_from * CHUNK_SIZE`, else starts at 0. Tested `test_resume_with_missing_file_acks_zero`. |
| E37 | Resume over a longer old part file | Stale tail bytes retained after shorter new file | Fixed | `server/server.py:257` `truncate(file_size)` on completion. Tested `test_resume_shorter_file_truncates_old_tail`. |
| E38 | Malformed RESUME payload | Handler crash | Fixed | `server/server.py:269` ignores payloads != 4 bytes. |
| E39 | CRC32 mismatch on a chunk | Corrupt chunk stored | Fixed | `server/server.py:244` drops mismatched chunk, no ACK → client retries/times out. Tested `test_server_rejects_corrupted_chunk`. |

## Phase 4 — Result & verification (bridge → dashboard)

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E40 | Bridge says `done` but file not on any server | Dashboard shows a false success | Fixed | `frontend/app.py:382` verifies `stored_files/h3|h4/<name>` after a done result; sets `verified`/`verification_error`; logs `[VERIFY]`. Tested `test_done_result_with_stored_file_is_verified` / `...without...`. |
| E41 | File landed on h4 but result names h3 | Verification wrongly fails | Fixed | Dashboard checks both servers (`frontend/app.py:384`). |
| E42 | Stale result file matched to a new run with same filename | Old result attributed to new run | Fixed | `_matches_current()` matches on `request_id` first, filename fallback (`frontend/app.py:100`). |
| E43 | Two identical event lines (retry, second connect attempt) | Log lines silently dropped | Fixed | Ingest by index, not string dedupe (`frontend/app.py:419`). |
| E44 | Progress/result file written half (dashboard killed mid-write) | Corrupt JSON crashes the endpoint | Fixed | Atomic `os.replace` writes (`frontend/app.py:128`, `mininet/bridge.py:73`); tolerant readers return `None`. |

## Phase 5 — SDN / link failure (Mininet + controller)

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E45 | Mesh broadcast storm (ARP floods loops) | `connect()` fails "No route to host", table-miss counts explode | Fixed | `mininet/topology.py:63` STP enabled on all switches before traffic. |
| E46 | Link killed mid-transfer; alternate path exists | Transfer stalls then dies instead of rerouting | Fixed | Controller Dijkstra recomputes on `port_status`; client `transfer_timeout` 60 s covers STP reconvergence; bridge `_wait_for_path` waits `LINK_RECOVERY_WAIT` (60 s) when process says alive. |
| E47 | Link killed and never restored | Transfer hangs forever | Fixed | `LINK_STALL_GRACE` (5 s) stall warning; bridge `CLIENT_TIMEOUT` (300 s) ends the run. |
| E48 | Link restored | Traffic stays on suboptimal path | Fixed | Controller recomputes shortest path on restore (Dijkstra); tested `test_recovery_restores_optimal_path`. |
| E49 | Server process alive but its path is down (blackhole) | Bridge calls server dead too early and failovers unnecessarily | Fixed | `mininet/bridge.py:419` `_wait_for_path` distinguishes process state from path state; waits for path recovery before declaring down. |
| E50 | Bridge launched outside Mininet / wrong namespace | All transfers fail "Network is unreachable" | Fixed | `mininet/bridge.py:639` `report_namespace()` prints observed IP and warns if not 10.0.0.1. |

## Phase 6 — Server kill / restart

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E51 | h3 killed mid-transfer, then restarted | Same file should resume and complete | Fixed | Retry with `resume_from` = resume point (`mininet/bridge.py:585`). Tested `test_resume_after_server_restart` scenarios in `docs/edge-cases.md`. |
| E52 | Server never returns | Failover to backup from chunk 0 | Fixed | `mininet/bridge.py:603` failover to `SERVERS[-1]` (or opposite). |
| E53 | Both servers down | Clear failure, no partial | Fixed | Tested `test_both_servers_unreachable_reports_clear_reason`. |
| E54 | Server-down confirmation race | Premature failover on a slow probe | Fixed | Two probes 5 s apart (`mininet/bridge.py:384` `_wait_for_server`). |
| E55 | Server restarted into a different storage dir | Old partial orphaned, resume target mismatch | Gap | Not handled; resume targets `stored_files/<host>` which is bind-mounted to `/tmp/backup`. |

## Phase 7 — Storage / quota

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E56 | Quota JSON `used_mb` stale (not recomputed from disk) | Failover to a server whose disk is actually full (the reported E20 bug) | Fixed | `mininet/bridge.py:356` `_disk_used_bytes()` reads `stored_files/<host>` live; `_server_free_bytes()` = quota − real disk usage. |
| E57 | `.part` files excluded from used-space display | Two transfers admitted, one fails at write | Fixed by server-side admission/write-time `OSError` (E32/E33). `.part` intentionally excluded from quota math to match server admission. |
| E58 | Quota set while a transfer runs | In-flight transfer unexpectedly re-routed | Fixed | E11. |

## Phase 8 — UI (dashboard)

| # | Case | Risk | State | Handling |
|---|------|------|-------|----------|
| E59 | All Transfer buttons in one line | Hard to read, accidental clicks, no visual grouping | Fixed in this change | Buttons regrouped into labeled rows (Transfer / Target / Link / Server / Storage) in `frontend/templates/dashboard.html` + `frontend/static/style.css` `.button-row` / `.group-label`. |
| E60 | Optimistic "Transferring…" before server confirms | Bar lies for a click's duration | Fixed by design | Reconciled on the next 2 s poll (`frontend/app.py:831` `/api/status`); `startInFlight` guard prevents double POST. |
| E61 | Storage bar shows full / low | Visual only | Fixed | `frontend/app.py:458` `storage-full`/`storage-low` host rect classes; badges in topology + metrics. |

## Gap summary (not yet implemented)

- G1: Stale `.part` auto-cleanup on bridge/server startup (E35).
- G2: Cross-server resume (resume_from carried to h4 after h3 dies) — failover restarts from 0 (E21).
- G3: Server restarted into a different storage dir breaks resume targeting (E55).

## Rubric mapping

| Case(s) | Rubric line |
|---------|-------------|
| E17, E51, E52 | Challenging scenario: service failure |
| E30, E33, E34 | Complete application functionality; data safety |
| E4, E30, E36 | Concurrency; failure handling |
| E26, E39 | Data integrity (per-chunk CRC32 + source consistency) |
| E45, E46, E48 | Dynamic SDN: rerouting on link failure/restore |
