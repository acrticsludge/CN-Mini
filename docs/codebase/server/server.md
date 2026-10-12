# `server/server.py` — TCP backup receiver

Multi-threaded receiver: one `ClientHandler` thread per connection, all
writing reassembled files into a storage dir. Runs on h3 and h4 (port 9000).

## `ClientHandler` (one thread per client)

- `run()` — loops `recv_frame` until disconnect, dispatching via
  `handle_frame`; `cleanup()` always closes the file handle and socket.
- `handle_frame(frame)` — routes HELLO / DATA / RESUME to `_handle_*`.
- `_handle_hello(frame)` — parses filename/size/`resume_from`, computes chunk
  count, opens the output file: **`r+b` + seek when resuming** an existing
  partial file, **`wb` (truncate) for fresh transfers** — so re-sending a
  shorter file over a longer stored one cannot leave the old tail behind
  (a real corruption bug fixed during build). Sends ready `ACK(0)`.
- `_handle_data(frame)` — verifies CRC32 **before** touching disk; on
  mismatch the chunk is dropped *without an ACK*, which the client reads as
  failure (same as a lost chunk — one code path for both). On match:
  seek → write → flush → advance position → `ACK(index)`. When received count
  reaches total, prints `[STORED] {filename, size, chunks}` (server-side log;
  see below) — the file is complete on disk at that point.
- `_handle_resume(frame)` — rewinds to `last_ack + 1`, seeks, confirms with
  `ACK(last_ack)` so both ends agree where to restart.
- `send_ack(i)` — 4-byte-index ACK frame. `cleanup()` — close file + socket.

## `BackupServer`

- `__init__(host, port, storage_dir)`; `start()` — `SO_REUSEADDR`, bind,
  `listen(5)`, accept loop spawning daemon `ClientHandler`s; `stop()` closes
  the listener. `__main__` takes `--host/--port/--storage`.

## The `[STORED]` subtlety (viva-ready)

`[STORED]` is printed by the **server process**. The bridge only sees the
**client's** stdout, so it can never observe `[STORED]` — completion is proven
client-side by the `[COMPLETE]` ACK tally + exit 0, and server-side by the
file appearing in `stored_files/` (bind-mounted from `/tmp/backup`). Anyone
proposing to "wait for `[STORED]` in the bridge" has misunderstood which
process prints it.
