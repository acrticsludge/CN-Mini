# `mininet/linkctl.py` — dashboard-driven link up/down

Kept separate from `topology.py` **so it imports without Mininet installed**
(everything operates on a duck-typed `net`); that is why it is unit-testable
on Windows while the rest of Mininet code is not.

- `apply_link_state(net, sw1, sw2, state)` — validates (`up`/`down`, known
  switches, distinct pair), then prefers `net.configLinkState(sw1, sw2, state)`
  because Mininet's own path emits the `OFPT_PORT_STATUS` event the Ryu
  controller listens for — a manual `ifconfig down` would break the link
  *without telling the controller*, silently defeating the demo. Falls back to
  per-interface `ifconfig` only if `configLinkState` is absent/fails. Returns
  `(ok, message)`.
- `read_json(path)` / `write_json_atomic(path, data)` — tolerant read (`None`
  on missing/corrupt); atomic write (`tmp` + `os.replace`) so the dashboard
  never reads half a confirmation.
- `serve_link_requests(net, request_file, result_file, poll=0.3,
  should_continue=None)` — main-thread loop: consume request (unlink first),
  apply, write result echoing the **`request_id`** so the dashboard can tell a
  fresh confirmation from a stale file. Runs until the CLI thread dies.

## Used by

- `topology.py` runs it on the main thread while the CLI owns a background
  thread — the only reason the dashboard buttons can break the real mesh.
- `frontend/app.py` `_request_link` is the other half of the protocol
  (5 s confirmation wait → 504 if Mininet never answers).
