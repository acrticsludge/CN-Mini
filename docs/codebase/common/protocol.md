# `common/protocol.py` — wire frame format

Single source of truth for every byte on the TCP connection. Both client and
server import from here, so the two ends cannot disagree on layout.

## Frame layout

```
+--------+------+-------+------------+----------+----------+---------+
| magic  | type | index | total_chks | checksum | data_len | payload |
| 4B     | 1B   | 4B    | 4B         | 4B       | 4B       | <=8KB   |
+--------+------+-------+------------+----------+----------+---------+
```

- `magic = 0xDEADBEEF` — sanity check; anything else is rejected.
- `type` — `0x01` DATA, `0x02` ACK, `0x03` RESUME, `0x04` HELLO.
- `index` — 0-based chunk number. `total_chks` — chunk count for the file.
- `checksum` — CRC32 of the payload only (header is trusted via magic).
- `data_len` — payload length, so the receiver knows exactly how many bytes
  complete the frame (TCP is a stream; frames need explicit lengths).
- `HEADER_FORMAT = '!IBIIII'` (network byte order), `HEADER_SIZE` = 21 bytes.
- `CHUNK_SIZE = 8192` — 8 KB payloads: small enough for quick ACK turnaround,
  large enough that a 1 GB file is ~131k frames, not millions.

## Functions

- `pack_frame(type, index, total, checksum, data_len, payload)` — builds
  header + payload. Every sender path funnels through this.
- `unpack_header(data)` — parses + validates magic; returns `None` on short
  input or bad magic instead of raising.
- `unpack_frame(data)` — header + sliced payload as a dict; `None` if invalid.
- `pack_hello(filename, file_size, resume_from=0)` — JSON payload
  (`filename`, `file_size`, `resume_from`). JSON keeps HELLO extensible without
  changing the binary layout; `resume_from` is the chunk index the client will
  start at (0 = fresh transfer → server truncates).
- `unpack_hello(payload)` — parses the HELLO JSON.
- `pack_data(index, total, chunk_data, checksum)` — one file chunk.
- `pack_ack(index)` / `unpack_ack(payload)` — 4-byte big-endian acked index.
  `unpack_ack` returns -1 on malformed payload instead of raising.
- `pack_resume(last_ack)` / `unpack_resume(payload)` — same 4-byte shape as ACK.

## Conversation (happy path)

```
client → HELLO{filename, file_size, resume_from} → server
client ← ACK(0)  "ready"                          ← server
client → DATA[i] (×N, one ACK each)               → server
```

RESUME frames are only sent when resuming (`resume_from > 0` and the client
already holds an ACK); see `client.md`.

## Pitfalls (hit during build)

- Returning `None` on bad input (not raising) is load-bearing: `recv_frame`
  propagates `None`, and both ends treat it as "connection gone", which is
  what turns a severed link into a clean failure instead of a crash.
