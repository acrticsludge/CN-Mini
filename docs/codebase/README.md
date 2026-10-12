# Codebase Map — Distributed Backup System

One page per core source file. Tests (`tests/`), caches, and data dirs
(`shared_files/`, `stored_files/`) are intentionally undocumented — they are
not part of the running system.

## Index

| Area | File | Doc |
|---|---|---|
| Wire protocol | `common/protocol.py` | [common/protocol.md](common/protocol.md) |
| Integrity | `common/checksum.py` | [common/checksum.md](common/checksum.md) |
| TCP framing | `common/transport.py` | [common/transport.md](common/transport.md) |
| Sender | `client/client.py` | [client/client.md](client/client.md) |
| Receiver | `server/server.py` | [server/server.md](server/server.md) |
| SDN brain | `controller/sdn_controller.py` | [controller/sdn-controller.md](controller/sdn-controller.md) |
| Virtual network | `mininet/topology.py` | [mininet/topology.md](mininet/topology.md) |
| h1 agent | `mininet/bridge.py` | [mininet/bridge.md](mininet/bridge.md) |
| Link switch | `mininet/linkctl.py` | [mininet/linkctl.md](mininet/linkctl.md) |
| Dashboard backend | `frontend/app.py` | [frontend/app.md](frontend/app.md) |
| Dashboard UI | `frontend/static/dashboard.js` | [frontend/dashboard-js.md](frontend/dashboard-js.md) |

`common/__init__.py` is an empty package marker — nothing to document.
`dashboard.html` / `style.css` are static presentation; the element-ID contract
they share with the UI logic is covered in `dashboard-js.md`.

## Runtime picture (3 terminals on the VM)

1. `sudo python3 mininet/topology.py` — builds the mesh, enables STP, mounts
   shared dirs into hosts, launches the bridge (h1) and both servers (h3, h4).
2. `ryu-manager controller/sdn_controller.py` — learns MACs, installs
   shortest-path flows, reroutes on link events.
3. `python frontend/app.py` — dashboard on port 5000.

## One click, end to end

1. Browser POSTs `/api/start-transfer` with a filename → Flask validates,
   rejects if busy (409), writes `transfer_request.json` with a `request_id`.
2. The bridge (polling on h1) consumes the request, marks progress running,
   spawns `client/client.py` against `10.0.0.3:9000, 10.0.0.4:9000`.
3. Client HELLOs → server ACKs ready → per-chunk DATA/ACK with CRC32 →
   client prints `[COMPLETE]` tally, exits 0 → bridge writes
   `transfer_result.json` with the same `request_id`.
4. Meanwhile the controller forwards every frame along Dijkstra paths; a link
   event triggers topology rebuild + flow replacement.
5. Browser polls `/api/status` (progress, chunks, throughput, bridge
   liveness) and `/api/transfer-result` (done/failed notice), and lists the
   reassembled file from `stored_files/` via `/api/files`.

## Side-channel files (`shared_files/`, bind-mounted at `/tmp/shared`)

`transfer_request.json`, `transfer_result.json`, `transfer_progress.json`,
`transfer_events.json`, `transfer_cancel.json`, `link_request.json`,
`link_result.json`, `bridge.lock`, `bridge_alive.json`. Plain JSON files are
the only bridge between Flask (host namespace) and Mininet hosts — there is
no RPC. Details live in `bridge.md` / `app.md`.

## Viva map

- "Why TCP / why chunks / why checksums?" → `protocol.md`, `checksum.md`, `client.md`
- "How does the transfer recover from interruption?" → `client.md` (RESUME), `server.md`
- "What does the controller actually do?" → `sdn-controller.md`
- "How is link failure demonstrated?" → `linkctl.md`, `topology.md`, `sdn-controller.md`
- "How do dashboard and Mininet talk?" → `bridge.md`, `app.md` (+ table above)
- "Why did X break during the build?" → each page's *Pitfalls* section
