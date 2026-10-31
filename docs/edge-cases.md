# Edge Cases — Distributed Backup System

This report covers failures that threaten a backup itself: the server dying, the sender dying, a failed retry wiping the last good copy, storage running out, two writers on one file, the source changing mid-transfer, and a "done" result that isn't actually on disk. Link failure is already handled by the SDN controller, so it isn't repeated here.

The focus is on cases a user can describe in one sentence. Protocol fuzzing, route flaps, and dashboard security are out of scope.

This is a plan. None of the target behavior below is implemented yet.

## Current baseline

- 72 tests pass on Windows in about 41 s (`python -m pytest tests -q`).
- Resume exists in the protocol. `HELLO` carries `resume_from`, and the server seeks to that chunk. Nothing invokes it automatically.
- The bridge runs the client once. There is no retry.
- The dashboard trusts the client's `[COMPLETE]` tally. It doesn't check the server-side file.

Those four facts explain most of the gaps below.

## Summary

| # | Case | Risk | Effort | Current state |
|---|---|---|---|---|
| 1 | Server dies mid-transfer | High | M | Fails; no retry |
| 2 | Sender crashes mid-transfer | Medium | S/M | Partial file exists; no resume control |
| 3 | Failed re-backup destroys the last good copy | High | S | Not handled |
| 4 | Server storage full or read-only | Medium | S | Opaque failure |
| 5 | Two clients write the same filename | High | S/M | Interleaved writes |
| 6 | Source changes during the backup | Medium | S | Not detected |
| 7 | Done, but the file isn't stored | High | S | Not verified |
| 8 | Both servers unreachable | Low | S | Works; untested |

---

## 1. Server dies mid-transfer

The full design, including the dashboard button and topology animation, is in [specs/server-failure-recovery.md](specs/server-failure-recovery.md).

**Scenario.** h3 goes down at chunk 40 of 128. The half file is in `stored_files/h3/`, because `/tmp/backup` is bind-mounted there.

**Today.** The client records `last_ack = 39`, prints `[CLIENT] No ACK received...`, and exits 1. This happens quickly when the server process dies; if the host blackholes, it waits out the 60 s window first. The bridge reports failed and stops. The partial file survives, but a new Start truncates it (case 3). No retry happens.

**Target.**

- The client prints `[RESUME_POINT] 39` before exiting.
- The bridge retries the same server with `--resume 40` after a short delay, up to two attempts.
- If the server stays down, the bridge retries h4 with `--resume 0` and labels the run "restarted on backup server".
- The dashboard shows the retry, then the resume or restart.

**Implementation notes.**

- `client/client.py`: emit the resume point. The separate `RESUME` frame isn't needed here; a fresh process has `last_ack = -1`, so the existing branch never fires. `HELLO` already carries the offset.
- `mininet/bridge.py`: `run_client` returns the resume point; `process_request` owns the retry loop. Reset the 300 s deadline per attempt, or keep one budget and subtract the waits.
- `server/server.py`: write at `frame['index'] * CHUNK_SIZE`, not at `_file_pos`. A retry after a lost ACK must overwrite the same chunk. Without this, retry corrupts the file.
- Failover to h4: h4 has no partial file. Passing `--resume N` makes the server open `wb` at offset 0 while the client starts at N. Pass 0, or make the ready ACK carry the server's accepted start chunk.

**Tests.**

- `test_resume_after_server_restart`: the server closes after 3 chunks; restart on the same storage; the client resumes at 3; the file matches.
- `test_retry_after_lost_ack_overwrites_same_chunk`: the server writes chunk 3, drops the ACK; the client retries; the file matches with no duplicate byte.
- `test_failover_to_backup_starts_from_zero`: h3 never returns; h4 receives the whole file; the result says "restarted".
- `test_both_servers_unreachable` (case 8).

**Acceptance.**

- [ ] Kill h3 at any chunk, restart it, and the same file completes.
- [ ] The dashboard shows retry and resume messages, not a generic failure.
- [ ] A lost ACK never duplicates a chunk.
- [ ] h4 failover produces a complete file and says it restarted.

---

## 2. Sender crashes mid-transfer

**Scenario.** The machine running the client dies. The server holds a partial file. Nobody knows the last good chunk.

**Today.** A new Start truncates the partial file and starts at zero. There is no Resume control.

**Target.**

- The dashboard finds a partial file in `stored_files/h3` or `h4` and offers "Resume from chunk N" or "Start over".
- N comes from the partial size: `floor(size / 8192)`, with a note when the last chunk is short.
- If the partial size equals the source size, the file is already complete; don't offer resume.
- Resume passes `--resume N`. Start over truncates, which case 3 makes safe.

**Implementation notes.**

- `frontend/app.py` already lists `STORED_DIR`. Add partial info to `/api/status` or a new `/api/partial`.
- `frontend/static/dashboard.js` renders the prompt and sends the resume value.
- `mininet/bridge.py` accepts `resume` in the request JSON.

**Tests.**

- `test_partial_file_offers_resume`
- `test_resume_from_partial_size_completes_file`
- `test_start_over_replaces_partial_file`

**Acceptance.**

- [ ] A partial file produces a Resume option with the right chunk.
- [ ] Resume doesn't re-send chunks the server already has.
- [ ] Start over still works.

---

## 3. Failed re-backup destroys the last good copy

**Scenario.** Yesterday's `thesis.docx` is on h3. Today's transfer fails at chunk 10. The server already truncated yesterday's copy when the new transfer started.

**Today.** `_handle_hello` opens the output with `wb` for a fresh transfer. The truncation happens before the first DATA arrives.

**Target.** Write to `thesis.docx.part`; `os.replace` to `thesis.docx` only after the last chunk is verified. The old file stays until then. Hide `.part` from the Stored Files panel, or label it "in progress".

**Implementation notes.**

- `server/server.py`: use a temp path for the output; `os.replace` on completion.
- Clean stale `.part` files on startup, or offer them as resume candidates (case 2).
- `frontend/app.py` `/api/files`: skip `.part` or mark it.
- Disk space: the transfer needs room for both versions. A server that is nearly full can fail a backup that would have fit in place.

**Tests.**

- `test_failed_rebackup_keeps_previous_file`
- `test_successful_rebackup_replaces_previous_file`
- `test_part_file_hidden_from_stored_list`

**Acceptance.**

- [ ] A failed re-backup leaves the previous file byte-identical.
- [ ] A successful re-backup replaces it in one step.
- [ ] The dashboard doesn't present a `.part` as a finished backup.

---

## 4. Backup server storage is full or read-only

**Scenario.** h3 fills at chunk 90, or the storage directory is read-only.

**Today.** The handler hits the error, logs it, and the connection closes. The client sees a closed connection, and the dashboard says "No ACK received", which points at the network, not the disk.

**Target.** Catch `OSError` at open, write, and flush. Send a clear error, either a new `MSG_ERROR` frame (type `0x05`, JSON payload) or a close with a logged reason. The client exits fast with `[CLIENT] Server storage error: ...`.

**Implementation notes.**

- `server/server.py`: wrap storage calls in `_handle_hello` and `_handle_data`; send the error and close.
- `common/protocol.py`: add `MSG_ERROR` and helpers if you take that route.
- `client/client.py`: handle `MSG_ERROR` while waiting for the ready ACK and during the chunk loop.
- Without the message type, map the close reason in the bridge. The dashboard text will be less specific.

**Tests.**

- `test_storage_full_fails_fast`: a fake file handle raises `OSError`; the client exits in under 2 s with a storage reason.
- `test_readonly_storage_reports_reason`
- `test_error_frame_roundtrip` if `MSG_ERROR` is added.

**Acceptance.**

- [ ] A storage failure is reported as storage, not as a timeout.
- [ ] The server keeps accepting other clients after one handler fails.
- [ ] The client doesn't hang for 60 s.

---

## 5. Two clients back up the same filename

**Scenario.** h1 and h2 both send `photo.jpg` to h3 at the same time.

**Today.** Both handlers open the same path and write at their own positions. The final file can mix both streams.

**Target.** Each transfer writes a unique temp file, or a per-filename lock rejects the second client. On completion, the last complete version replaces the final name atomically. The stored file is always one complete version.

**Implementation notes.**

- Unique temp: `filename.<pid>-<thread>.part`, then `os.replace`.
- Lock: a `threading.Lock` per filename; the second client waits or gets a "file busy" error.
- The temp approach is smaller and needs no new protocol message. The lock gives a clearer message.

**Tests.**

- `test_concurrent_same_filename_never_mixes`: two threads, different content, same name; the final file equals one complete version.
- `test_second_client_gets_busy` if you add the lock.

**Acceptance.**

- [ ] The final file is byte-identical to one sender's content.
- [ ] No interleaved bytes.
- [ ] The dashboard shows one stored file, not a corrupted one.

---

## 6. Source file changes during the backup

**Scenario.** Someone edits `report.csv` while it is being sent. Each chunk is valid, so the stored file is half old and half new.

**Today.** Each chunk's CRC is checked on its own. There is no file-level check.

**Target.** Record `st_size` and `st_mtime_ns` before the first chunk; re-check after the last. If either changed, print `[INCONSISTENT] Source changed during backup` and exit 1.

**Implementation notes.**

- `client/client.py`: `os.stat` before and after.
- `mininet/bridge.py`: surface the `[INCONSISTENT]` line as the failure reason.
- Caveat: mtime can be coarse. If the size is unchanged and the edit lands in the same tick, the check can miss it. Say so. A hash before and after is stronger but reads the file twice.

**Tests.**

- `test_source_changed_during_transfer_is_rejected`
- `test_unchanged_source_completes`

**Acceptance.**

- [ ] A changed source fails the run.
- [ ] The dashboard says the source changed, not "checksum failed".
- [ ] An unchanged file still completes.

---

## 7. Done, but the file is not on the server

**Scenario.** The dashboard says done, but `stored_files/` is empty. Maybe the file landed on h4, or it was removed after the transfer.

**Today.** The bridge trusts exit 0 plus `[COMPLETE]`. Nothing checks the server-side copy.

**Target.** When a result says done, Flask checks `stored_files/h3/<name>` and `stored_files/h4/<name>`. If neither exists with the expected size, mark the run "done but not verified" and log `[VERIFY]`.

**Implementation notes.**

- `frontend/app.py`: after `_sync_transfer_state` sees a done result, verify both server dirs. Add `verified` and `verification_error` to `transfer_state`.
- `frontend/static/dashboard.js`: show "done (verified)" or "done, copy not found".
- The source size is in `shared_files/<name>` while it exists. If the source is gone, compare the chunk count or skip the size check.

**Tests.**

- `test_done_result_without_stored_file_is_flagged`
- `test_done_result_with_stored_file_is_verified`
- `test_done_result_on_h4_is_verified`

**Acceptance.**

- [ ] A missing stored file can't show as a clean success.
- [ ] A file on either server counts as verified.
- [ ] The event log names the failed check.

---

## 8. Both servers unreachable before the backup starts

**Scenario.** h3 and h4 are both down.

**Today.** The client tries each with a 10 s connect timeout, fails after up to about 20 s, and exits 1. The bridge shows the last connect error. The behavior is reasonable, but no test pins it.

**Target.** Keep the behavior, add a final `[CLIENT] No backup server reachable`, and test it.

**Tests.**

- `test_both_servers_unreachable_reports_clear_reason`

**Acceptance.**

- [ ] The run fails in about 20 s, not 60 s.
- [ ] The dashboard says no server is reachable.
- [ ] No partial file is created.

---

## Test matrix

| File | New tests | Needs Mininet? |
|---|---|---|
| `tests/test_edge_cases.py` (new) | server restart, lost ACK, storage full, concurrent same name, source changed, both servers down | No |
| `tests/test_recovery.py` | resume after restart, failover start-from-zero | No |
| `tests/test_bridge_request.py` | retry loop, resume point parsing | No |
| `tests/test_dashboard_busy.py` | partial info, stored-file verification | No |
| `tests/test_dashboard_ui.py` (new, optional) | retry, resume, verification messages | Playwright |

That's about 18 new tests. The suite goes from 72 to roughly 90. All backend tests can use `tmp_path` and fake sockets, the same way the current tests do. Only the two demo scripts below need a live Mininet network.

## Implementation order

| Step | Case | Why here |
|---|---|---|
| 1 | 3 | Smallest change, protects existing data, makes retries safer |
| 2 | 1 | The requested scenario; needs the resume point and the bridge retry |
| 3 | 4 | Turns an opaque failure into a clear one |
| 4 | 5 | Unique temp files follow naturally from case 3 |
| 5 | 2 | Dashboard resume reuses the resume point from case 1 |
| 6 | 6 | Source consistency is independent |
| 7 | 7 | Verification is independent and small |
| 8 | 8 | Test only |

Doing case 3 before case 1 matters. A retry that fails must not destroy the old copy. Doing case 1 before case 2 matters because the sender-crash UI needs the same resume point.

## Demo scripts

### Demo A: server dies mid-transfer

1. Create a 50–100 MB file in `shared_files/`.
2. Start the transfer. Wait until the bar reaches about 30%.
3. `mininet> h3 pkill -f server.py`
4. The dashboard shows "server unreachable, retrying".
5. Restart the server with the same command `topology.py` uses:
   `mininet> h3 python3 /tmp/src/server/server.py --host 10.0.0.3 --port 9000 --storage /tmp/backup &`
6. The dashboard shows "resumed at chunk N".
7. When it finishes, compare hashes:
   `sha256sum shared_files/<file> stored_files/h3/<file>`

If `&` doesn't background the process in your shell, use `nohup ... &` or run the server from a second terminal with `mininet/util/m h3 ...`.

### Demo B: failed re-backup keeps the old copy

1. Back up `thesis.docx` and confirm it in `stored_files/h3/`.
2. Start a second transfer of the same file. Kill it at about 50% with Stop or `h3 pkill`.
3. `ls -l stored_files/h3/` shows `thesis.docx` intact and `thesis.docx.part`.
4. Resume. When it finishes, `thesis.docx` is replaced and the `.part` is gone.

## Risks and limits

- Temp plus rename can nearly double the disk space during a transfer. A nearly full server can fail a backup that would have fit in place. Mention it in the runbook.
- h4 failover starts from zero. True cross-server resume needs replication, which the project already lists as future work.
- The source-change check can miss a same-size, same-mtime edit. State that limit instead of claiming full detection.
- The 60 s ACK window is the retry budget. A server that takes longer than that to return fails the run. The bridge's 300 s deadline must cover all attempts, or reset it per attempt.
- Adding `MSG_ERROR` changes the wire protocol. An old client would wait for an ACK and time out. Note the compatibility in the protocol doc.
- `.part` files can accumulate after a crash. Clean them at startup or expose them as resume candidates.
- Verification reads `stored_files/` from the host. The bridge on h1 can't do this. Keep the check in Flask.

## Rubric mapping

| Case | Rubric line |
|---|---|
| 1, 8 | Challenging scenario: service failure |
| 3, 7 | Complete application functionality; data safety |
| 4 | Service failure; error handling |
| 5 | Concurrency; failure handling |
| 6 | Data integrity |

## Open questions

- Do you want `MSG_ERROR`, or is a fast close enough?
- Should the second client wait, or be rejected with "file busy"?
- Is cross-server resume in scope, or is restart-on-h4 acceptable?
- Do you want a SHA-256 file hash in the result, or is size plus mtime enough?
- Should `.part` files show in the dashboard as "in progress"?

## Definition of done

- [ ] `python -m pytest tests -q` passes with the existing 72 tests and the new ones.
- [ ] The two demo scripts run on the VM.
- [ ] The dashboard event log shows retry, resume, storage, and verification messages.
- [ ] `docs/test-cases.md` lists the new cases next to the existing suite.
