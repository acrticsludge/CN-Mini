# Handoff Prompt — Build the Distributed Backup System

You are implementing a complete **Distributed Backup System** from scratch. This is a greenfield build — no code exists yet.

## Read These First

1. `C:\Anubhav\CN-Mini\CLAUDE.md` — project rubric and requirements (40 marks)
2. `C:\Anubhav\CN-Mini\docs\reasonix\specs\2026-01-07-distributed-backup-design.md` — the agreed design
3. `C:\Anubhav\CN-Mini\docs\runbook\MASTER.md` — how the system runs, tech stack, principles
4. `C:\Anubhav\CN-Mini\README.md` — what the project does

## What to Build

Every file in this structure:

```
distributed-backup-system/
├── mininet/
│   └── topology.py                 # 4 OVS switches, 4 hosts, partial mesh, bind-mount stored_files/
├── controller/
│   └── sdn_controller.py           # Ryu app: topology discovery, Dijkstra, flow rules, port status events
├── client/
│   └── client.py                     # TCP client: chunk file (8KB), CRC32, send, RESUME on reconnect
├── server/
│   └── server.py                   # TCP server: receive, verify CRC32, reassemble, write to /tmp/backup/
├── common/
│   ├── protocol.py                 # Binary frame format: magic, type, index, total, checksum, len, payload
│   └── checksum.py                 # CRC32 wrapper
├── frontend/
│   ├── app.py                      # Flask: REST API + WebSocket + /api/files + /api/start + /api/link-down
│   ├── templates/
│   │   └── dashboard.html          # Single-page: topology SVG, transfer control, metrics, stored files, event log
│   └── static/
│       ├── style.css               # Clean, minimal styling
│       └── dashboard.js            # Render topology, charts (Chart.js), WebSocket events, file listing
├── tests/
│   ├── test_transfer.py            # Client sends file, server reassembles, diff matches
│   ├── test_recovery.py            # Kill client mid-transfer, restart with RESUME, file completes
│   └── test_failure.py             # Link down event triggers rerouting, transfer resumes
├── stored_files/                   # Created by topology.py, persists across Mininet restarts
│   ├── h3/
│   └── h4/
├── docs/
│   ├── architecture.md             # How it works, diagrams
│   ├── test-cases.md               # Test scenarios + expected results
│   └── results.md                  # Performance metrics, comparison table
└── requirements.txt                # ryu, flask, flask-socketio, pytest, matplotlib
```

## Implementation Order

Build in this order — each phase depends on the previous:

| Phase | Files | What to verify before moving on |
|-------|-------|-------------------------------|
| **1** | `common/protocol.py`, `common/checksum.py` | Frame pack/unpack round-trips, CRC32 matches |
| **2** | `server/server.py` | Start server, send raw bytes with netcat, verify it receives |
| **3** | `client/client.py` | Client sends a file, server reassembles, `diff` matches original |
| **4** | `mininet/topology.py` | `sudo python topology.py`, `pingall` works, `stored_files/` dirs created |
| **5** | `controller/sdn_controller.py` | `ryu-manager` starts, `ovs-ofctl dump-flows s1` shows flows |
| **6** | Recovery logic in `client/` + `server/` | Kill client mid-transfer, restart with `--resume`, file completes |
| **7** | Dynamic path selection in `controller/` | `link s1 s2 down` in Mininet, traffic reroutes |
| **8** | `frontend/app.py`, `dashboard.html`, `style.css`, `dashboard.js` | Open browser, see topology, click buttons, watch events |
| **9** | `tests/` | `pytest tests/` all pass |
| **10** | `docs/architecture.md`, `docs/test-cases.md`, `docs/results.md` | Written with real data |

## Key Design Decisions (from the spec)

- **Language:** Python 3.8+
- **SDN Controller:** Ryu (not POX, not OpenDaylight)
- **Topology:** 4 switches (OVS, OpenFlow 1.3), 4 hosts in partial mesh
- **Chunk size:** 8 KB
- **Checksum:** CRC32 (`zlib.crc32`)
- **Protocol:** Binary header + payload (not JSON)
- **Recovery:** RESUME message with last-ACKed chunk index
- **Multi-server:** Primary-backup failover (client tries h3, then h4)
- **Concurrency:** Server uses threading to handle multiple simultaneous clients
- **Error handling:** Try/except on all socket operations, CRC32 mismatch detection, connection timeout handling, graceful degradation
- **Persistence:** `stored_files/h3/` and `stored_files/h4/` bind-mounted into Mininet at `/tmp/backup/`
- **Frontend:** Flask + vanilla JS + SVG + Chart.js, runs on host at `localhost:5000`
- **Challenging scenario:** Link failure + automatic rerouting

## Rubric Alignment (40 marks total)

The implementation must explicitly address each rubric criterion:

| Rubric | Marks | How this project addresses it |
|--------|-------|------------------------------|
| **Problem Understanding & Architecture** | 2 | Clear architecture diagram, well-defined components, communication flow in `docs/architecture.md` |
| **TCP/UDP Socket Implementation** | 4 | Low-level TCP sockets (`socket.SOCK_STREAM`), client/server model, threading for concurrency, error handling on all ops, custom binary protocol with message types (HELLO, DATA, ACK, RESUME) |
| **Mininet + SDN Implementation** | 4 | 4-switch partial mesh topology, Ryu controller with LLDP discovery, proper flow rules via OpenFlow 1.3, host/switch connectivity verified with `pingall` |
| **Socket–SDN Integration** | 3 | Controller observes network events and reroutes TCP traffic; dashboard shows both socket transfer and SDN state side-by-side; end-to-end integration demonstrated when link failure causes visible rerouting during active transfer |
| **Initial Testing & Demo** | 2 | `pytest tests/` covers basic transfer, recovery, and failure scenarios; all tests pass before demo |
| **Complete Application Functionality** | 4 | All 8 required features implemented: TCP transfer, chunking, checksums, resume, multiple servers, path selection, failure simulation, metrics |
| **Dynamic SDN Functionality** | 6 | Controller implements: **rerouting** (link failure → Dijkstra recomputation), **monitoring** (flow stats polling every 5s), **recovery** (automatic path restoration when link comes back). Dashboard shows dynamic behavior live. |
| **Challenging Scenario / Robustness** | 4 | Link failure during active transfer — controller detects via OpenFlow port status, reroutes via alternate path, transfer resumes with zero data loss |
| **Performance Evaluation & Comparison** | 5 | Throughput + recovery time measured; experiments: normal transfer, link failure with SDN, link failure without SDN (static baseline); comparison table in `docs/results.md` |
| **Documentation, Final Demo & Viva** | 6 | README, architecture.md, test-cases.md, results.md, MASTER.md, spec — all written; reproducible demo from clean clone; viva prep: understand every file and design decision |

### Dynamic SDN Mechanisms (must implement at least 3)

The rubric lists: routing, filtering, prioritization, load balancing, rerouting, monitoring, blocking, recovery.

This project implements:
1. **Rerouting** — Link failure triggers Dijkstra recomputation and new flow rules
2. **Monitoring** — Controller polls flow stats every 5s to detect congestion
3. **Recovery** — When a link comes back, controller restores optimal path
4. **Routing** — Dijkstra shortest-path computation for all flow installations

### Socket-SDN Integration Points

The rubric says "application/network events influence SDN behavior where required." This project demonstrates integration at these points:

1. **Network event → SDN action:** Link down event triggers controller rerouting (visible during active transfer)
2. **Application state → Dashboard:** Transfer progress, chunk count, and throughput shown live alongside network state
3. **End-to-end demonstration:** Teacher clicks "Trigger Link Failure" during a transfer and watches the controller reroute while the transfer continues — both layers visible simultaneously

## Acceptance Criteria

The project is done when:

1. `sudo python mininet/topology.py` starts Mininet with 4 switches and 4 hosts
2. `ryu-manager controller/sdn_controller.py` installs flow rules on all switches
3. `python frontend/app.py` serves a dashboard at `http://localhost:5000`
4. Clicking **Start Transfer** on the dashboard sends a file from client to server
5. The file appears in `stored_files/h3/` on the host — and survives Mininet restart
6. Clicking **Trigger Link Failure** kills a link, the controller reroutes, and the transfer resumes
7. The dashboard shows: live topology, throughput chart, recovery time, stored files, event log
8. `pytest tests/` passes all tests
9. `docs/results.md` has real throughput and recovery time numbers
10. The whole system runs on **one Ubuntu VM** — no extra hardware

## Constraints

- **TCP only** — no UDP
- **Everything runs in Mininet** — no real network
- **Python** — no other language
- **One VM** — no Docker, no cloud, no extra machines
- **Reproducible** — anyone can clone the repo and run it with the README instructions
- **Student project** — impressive but achievable, not over-engineered

## Tips

- Test each phase on localhost before moving to Mininet (phases 1-3 don't need Mininet)
- Use `ovs-ofctl dump-flows s1` to debug flow rules
- Use `tcpdump -i s1-eth2` to watch packets reroute
- The Ryu controller API is well-documented — use `ryu.app.ofctl_rest` for REST access
- For the dashboard, keep it simple — a single HTML page with vanilla JS is enough
- `matplotlib` is for generating result charts, not for the live dashboard (that's Chart.js)

## Output

When done, the repo should be ready to demo:
- `README.md` with setup steps
- All source code
- All docs (architecture, test cases, results)
- A working test suite
- A recorded demo video (optional but impressive)
