# `common/transport.py` — TCP stream → frames

TCP delivers a byte *stream*, not messages. This module is the only place that
turns the stream back into protocol frames. Both client and server use it.

- `recv_all(sock, n)` — loops `recv` until exactly `n` bytes arrive.
  Returns `None` on disconnect (`recv` → `b''`) or any `OSError`.
- `recv_frame(sock)` — reads `HEADER_SIZE` bytes, validates via
  `unpack_header` (`None` on bad magic), then reads `data_len` payload bytes.
  Returns the frame dict, or `None` if anything is off.

## Consequences worth knowing

- `socket.timeout` is an `OSError` subclass, so a timeout surfaces as `None`,
  not an exception. The client's 10 s socket timeout therefore reads as "no
  ACK" → clean transfer failure, which is what the dashboard shows during a
  link blackhole instead of a hung client.
- `recv_frame` can return a frame whose `payload` is `None` (header arrived,
  payload didn't). Callers must guard before unpacking — the client unpacks
  ACK payloads with `struct.unpack`, so a mid-payload disconnect would raise
  there; in practice the `None`-frame check above it catches the common case.
- All reads are blocking with the socket's timeout; there is no async I/O
  anywhere in the data path. Concurrency comes from threads (server handlers)
  and processes (bridge-spawned clients), not from the transport.
