# Test Cases — Distributed Backup System

## Test Suite: `tests/`

### `test_transfer.py` — Basic File Transfer

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `test_crc32_computation` | Verify CRC32 checksum computation | Correct checksum for known input |
| `test_crc32_detects_corruption` | Verify CRC32 catches data corruption | Corrupted data fails verification |
| `test_pack_unpack_frame` | Frame pack/unpack round-trip | All fields preserved |
| `test_pack_unpack_hello` | HELLO message encoding/decoding | Filename and size preserved |
| `test_pack_unpack_ack` | ACK message encoding/decoding | Chunk index preserved |
| `test_pack_unpack_resume` | RESUME message encoding/decoding | Resume index preserved |
| `test_invalid_magic_rejected` | Frame with wrong magic bytes | Returns None |
| `test_short_frame_rejected` | Truncated frame data | Returns None |
| `test_basic_transfer` | Client sends 16KB file to server | Received file matches original |
| `test_multiple_chunks` | Client sends 40KB file (5 chunks) | All chunks received correctly |
| `test_server_rejects_corrupted_chunk` | Send chunk with wrong CRC32 | Server drops corrupted chunk |

### `test_recovery.py` — Interrupted-Transfer Recovery

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `test_resume_after_partial_transfer` | Send 3 chunks, crash, resume from chunk 3 | File completes correctly |
| `test_resume_from_zero` | Resume from chunk 0 | Equivalent to fresh transfer |
| `test_server_handles_multiple_clients` | 3 concurrent clients send files | All transfers succeed |

### `test_failure.py` — Path Failure & SDN Rerouting

| Test | Description | Expected Result |
|------|-------------|-----------------|
| `test_controller_handles_port_status` | Controller has required handler methods | All methods present |
| `test_topology_graph_construction` | Dijkstra on simple graph | Correct shortest path |
| `test_link_failure_removes_path` | Remove edge, recompute | Alternate path found |
| `test_recovery_restores_optimal_path` | Restore edge, recompute | Optimal path restored |
| `test_frontend_api_endpoints` | Flask app has all routes | All 8 API routes present |
| `test_stored_files_directory_exists` | Directory structure created | h3/ and h4/ exist |

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_transfer.py -v
pytest tests/test_recovery.py -v
pytest tests/test_failure.py -v

# Run with coverage
pytest tests/ --cov=. --cov-report=term-missing
```

## Expected Results

All tests should pass. The test suite covers:

1. **Protocol correctness** — frame encoding/decoding, CRC32 integrity
2. **File transfer** — single-chunk and multi-chunk transfers
3. **Error handling** — corrupted chunks, invalid frames
4. **Recovery** — resume after partial transfer, concurrent clients
5. **SDN logic** — topology graph, path computation, link failure recovery
6. **Frontend** — API endpoint availability, file system structure

## Server failure recovery (edge case 1)

New tests:

- `tests/test_transfer.py` — duplicate chunk, out-of-order chunk, resume with a missing file, resume with a partial file, empty file.
- `tests/test_recovery.py` — client resume point, server accepted start.
- `tests/test_bridge_retry.py` — same-server retry, failover, cancel during the probes, progress held during the retry, timer heartbeats during ACK stalls, process-aware path waits, and failover only when the server is dead/unknown or the path wait expires.
- `tests/test_serverctl.py` — kill, restart, idempotence, status, two probes.
- `tests/test_dashboard_server.py` — server routes, status payload, retry phase, recovery label, `.part` hidden from `/api/files`.
- `tests/test_e2e_local.py` — a real client resumes after the server dies and a new one starts.
- `tests/test_transfer.py` — a completed transfer promotes `.part` to the final name; a shorter resume truncates the stale tail.

Manual demo: kill h3 mid-transfer, restart it before the second probe, then compare the source and stored hashes. Leave h3 down to see the run restart on h4.

Manual link-failure check: kill s1-s2 mid-transfer, confirm the bridge stays alive during the ACK stall, confirm no h4 failover while `server_status.json` still marks h3 alive, then confirm the same run resumes or—if the path stays down—fails over only after the path wait.

## Concurrent same-filename backup (edge case 5)

`tests/test_edge_cases.py` covers the second writer:

- `test_server_rejects_second_transfer_for_same_name` — a stalled first transfer holds the name; the second gets `MSG_ERROR` with `code=busy`; the first still produces a complete file; the claim is released for the next transfer.
- `test_client_labels_busy_error` — the client prints `[CLIENT] File busy: ...`, not `Server storage error`, so the bridge does not treat a busy file as a full disk.
- `test_server_rejects_part_suffixed_filename` — a source named `x.part` is refused with `code=invalid`, so it cannot collide with the temp path for `x`.
- `test_dashboard_hides_and_rejects_part_sources` — the picker hides `.part` names and `/api/start-transfer` returns 400 for them.

## Failed re-backup keeps previous file (edge case 3)

`tests/test_edge_cases.py` covers:

- `test_failed_rebackup_keeps_previous_file` — the server writes to `.part` and promotes only after the last chunk; a kill mid-transfer leaves the final copy byte-identical and only a `.part` remains.
- `test_successful_rebackup_replaces_previous_file` — a successful re-backup replaces the previous file in one atomic step.
- `test_part_file_hidden_from_stored_list` — the dashboard `/api/files` hides `.part` names.

## Source changes during backup (edge case 6)

`tests/test_edge_cases.py` covers:

- `test_source_changed_during_transfer_is_rejected` — a fake server delays the first ACK, the source is mutated mid-transfer, and the client exits with `[INCONSISTENT] Source changed during backup` instead of reporting success.
- `test_unchanged_source_completes` — an unchanged source completes and is stored correctly.

## Both servers unreachable (edge case 8)

`tests/test_edge_cases.py` covers:

- `test_both_servers_unreachable_reports_clear_reason` — with nothing listening, the client prints `[CLIENT] No backup server reachable`, exits 1, and creates no partial file.

## Failover storage safety (edge case 20)

`tests/test_edge_cases.py` covers the reported bug — kill h4 mid-transfer, failover swaps to h3 with less space:

- `test_bridge_failover_blocks_when_backup_disk_is_full` — h3 disk holds 9 MB of files but quota JSON says `used_mb=0`; a 5 MB file failing over to h3 is **blocked**, `run_client` is never invoked with h3's address, run fails with `[STORAGE] h3 also lacks space`.
- `test_bridge_failover_proceeds_when_backup_disk_has_space` — h3 quota JSON says `used_mb=10` (looks full) but disk is empty; a 5 MB file failing over to h3 **proceeds** — stale JSON never blocks a valid failover.

## Resume-from validation (edge case 31)

`tests/test_edge_cases.py` covers:

- `test_bridge_clamps_resume_from_beyond_total_chunks` — `resume_from=100` on a 10-chunk file is clamped to 10 (fresh transfer), preventing a seek-past-EOF phantom success.

## `.part` disk counting (edge case 32)

`tests/test_edge_cases.py` covers:

- `test_bridge_counts_part_files_in_failover_disk_check` — a 9 MB orphaned `orphan.bin.part` on h3 counts against disk; a 5 MB file failing over to h3 is blocked even though quota JSON says `used_mb=0`.

## Source deleted mid-transfer (edge case 30)

`tests/test_edge_cases.py` covers:

- `test_client_source_file_deleted_mid_transfer` — the source file is deleted mid-transfer; client fails fast with `[RESUME_POINT]` instead of crashing, no partial result reported as success.
