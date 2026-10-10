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
