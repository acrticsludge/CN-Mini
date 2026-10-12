# `client/client.py` — TCP backup sender

Reads a file, splits it into 8 KB chunks, sends each with its CRC32, and waits
for a per-chunk ACK (stop-and-wait). Spawned by the bridge as a subprocess,
one process per transfer.

## `BackupClient`

- `__init__(servers, chunk_size, connect_timeout=10, transfer_timeout=60)` —
  `servers` is an ordered list of `(host, port)`; `last_ack` tracks recovery
  state (`-1` = nothing acked). The two timeouts are deliberately different
  (see below).
- `connect()` — tries each server **in order**, `connect_timeout` per server,
  closing failed sockets. First success wins (normally h3; h4 is the failover).
  Short on purpose: a truly dead server should fail fast. Every failure is
  printed as `[CLIENT] Failed to connect to host:port: ...` — the line the
  bridge scrapes to explain failures on the dashboard.
- `send_file(filepath, resume_from=0)` — right after connecting, widens the
  socket timeout to `transfer_timeout` (default 60 s), then:
  1. Sends `HELLO{filename, file_size, resume_from}` (fresh transfers pass 0,
     telling the server to truncate).
  2. Waits for the ready `ACK`; anything else aborts.
  3. If resuming, sends `RESUME(last_ack)` and waits for its ACK.
  4. Delegates to `_send_chunks`.

## Why two timeouts (mid-transfer link death)

A dead link blackholes ACKs for tens of seconds (controller reroute is fast;
STP unblocking the alternate path is not). The 10 s connect timeout would kill
the run mid-stall — before the reroute it was meant to use. The 60 s transfer
timeout lets the run stall while TCP retransmits, then continue on the healed
path with zero rework; the byte stream resumes exactly (TCP guarantees it, the
server just sees delayed frames). Tested with a 12 s ACK blackhole
(`TestStallRecovery`). CLI flags: `--connect-timeout`, `--transfer-timeout`;
the bridge passes `--transfer-timeout` from `BRIDGE_TRANSFER_TIMEOUT`.
- `_send_chunks(filepath, start_chunk, total_chunks)` — seeks to the start
  chunk, then per chunk: read → CRC32 → `sendall(DATA)` → block for ACK →
  update `last_ack` / byte counters → print a machine-readable
  `[PROGRESS] {chunk, total, bytes, throughput}` line. A lost connection or
  missing ACK records `last_ack = index - 1` and returns `False` (the resume
  point for a retry). On success prints `[COMPLETE] {filename, chunks, bytes,
  throughput}` (the tally the bridge trusts) plus a human summary line.
- `_report_progress(...)` — the `[PROGRESS]` emitter; human-readable percent
  lines only every 10th chunk to keep logs readable on 131k-chunk files.
- `close()` — closes the socket, clears `connected`.

## `__main__` (CLI)

`client.py FILE --servers h1:p1 h2:p2 --resume N`. Splits `host:port` strings
into tuples, runs one transfer, then **`sys.exit(0 if ok else 1)`**.
The exit code is a contract, not a nicety: an early version always exited 0,
so failed transfers (server unreachable) were reported as successes. The
bridge requires exit 0 **and** the `[COMPLETE]` tally before calling it done.

## Pitfalls (hit during build)

- `[STORED]` is printed by the *server*, so scraping the *client's* stdout
  for it (as the bridge once did) never fires. Done-ness = exit code +
  `[COMPLETE]`; the Stored Files panel is the server-side proof.
- Stop-and-wait ACK per chunk is simple and correct but slow: a 1 GB file is
  ~131k round-trips. Throughput improvements (bigger chunks, pipelining) would
  change protocol behavior — deliberately untouched.
