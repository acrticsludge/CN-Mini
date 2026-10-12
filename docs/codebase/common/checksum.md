# `common/checksum.py` — per-chunk integrity

Two functions, zero dependencies (uses stdlib `zlib`).

- `compute_crc32(data)` — returns `zlib.crc32(data) & 0xFFFFFFFF`.
  The mask matters: `zlib.crc32` can return a *signed* int in Python, which
  would pack/unpack inconsistently in the 4-byte header checksum field.
  Masking forces unsigned 32-bit, matching the `I` in `HEADER_FORMAT`.
- `verify_crc32(data, expected)` — equality check, used by the server on every
  DATA chunk before writing it to disk.

## Why CRC32 and not a hash

8 KB chunks × ~131k per GB: CRC32 is hardware-fast and catches random
bit-flips, which is the threat model on a (virtual) wire. It is not a
cryptographic guarantee — for the viva: integrity here means *corruption
detection*, and a mismatched chunk is dropped without an ACK, so the client
treats it exactly like a lost chunk.

## Used by

- `client/client.py` — computes per chunk before `pack_data`.
- `server/server.py` — verifies in `_handle_data` before writing/ACKing.
