# CN-Mini Master Document — Distributed Backup System

*One doc, end to end: what the project is, what it's built with, and how a
file gets from a click to safe storage across a self-healing network. Technical,
but systems-level — for per-file detail see `docs/codebase/`.*

## 1. What this is

A client backs up files to redundant servers over **TCP**, inside a virtual
network whose paths are chosen by an **SDN controller**. Files are split into
checksummed chunks with resume support; when a link dies mid-transfer, the
controller reroutes and the run stalls, then continues — no restart. A web
dashboard shows transfers, metrics, and events live.

**One line:** TCP file backup with chunking, checksums and resume, running on
a Mininet mesh that heals itself via a Ryu controller, visualized on a Flask
dashboard.

## 2. Frameworks (what each one does here)

| Framework | Role in this project | Why it |
|---|---|---|
| **Python 3** | Everything — sockets, controller app, dashboard, tests | Course stack, Ryu/Flask/Mininet APIs are Python |
| **Mininet** | Fakes the whole network (4 OVS switches + 4 hosts) in one VM | Reproducible topology without hardware |
| **Open vSwitch (OVS)** | The actual switch software, speaking OpenFlow 1.3 | What real SDN testbeds use |
| **Ryu** | The SDN controller: learns topology, installs paths, reroutes on failure | Lightweight, Python, event-driven |
| **Flask + vanilla JS** | Dashboard backend (REST) + live UI (polling, SVG topology, Chart.js) | No build step, runs anywhere |
| **zlib CRC32** | Per-chunk integrity fingerprints | Fast enough for 8 KB × 144k chunks |
| **pytest** | 70+ tests: protocol, transfer, resume, reroute, dashboard races, end-to-end | Guards every bug class met during build |

No Docker, no cloud, no paid services. Three terminals on an Ubuntu VM is the
entire deployment.

## 3. System map

```
Hosts:  h1=10.0.0.1, h2=10.0.0.2 (clients)   h3=10.0.0.3, h4=10.0.0.4 (servers)
Switches: s1–s4, full mesh (6 inter-switch links), STP enabled (loop-free broadcast)
Ports:  TCP :9000 (backup traffic) · :6633 (controller↔switches) · :5000 (dashboard)

Terminal 1: sudo python3 mininet/topology.py   → builds net, launches bridge (h1) + servers (h3, h4)
Terminal 2: ryu-manager controller/sdn_controller.py → learns MACs, installs Dijkstra flows
Terminal 3: python frontend/app.py             → dashboard at localhost:5000
```

File sharing across namespace boundaries is done with **bind mounts**:
`stored_files/h3|h4` ↔ `h3|h4:/tmp/backup` (received files land in the repo),
`shared_files/` ↔ `/tmp/shared` on all hosts (the JSON side-channel below).

### The mesh (all 6 inter-switch links drawn)

```
         h1                  h3
         |                   |
         s1 ======= s2
         ||\       /||
         || \     / ||
         ||  \   /  ||
         ||   \ /   ||
         ||   / \   ||
         ||  /   \  ||
         || /     \ ||
         ||/       \||
         s3 ======= s4
         |           |
         h2          h4
```

Top row, sides, bottom row, plus the crossing diagonals = 6 links. Kill any
one and a 2-hop path remains — that redundancy is the whole demo.

### The two planes

```
  DATA PLANE — file bytes, TCP :9000
  [ h1 / h2 clients ] <=== chunks + CRC32 / ACKs ===> [ h3 / h4 servers ]

  CONTROL PLANE — decisions, OpenFlow 1.3
  [ Ryu controller ] ---- flow rules ----> [ s1 s2 s3 s4 ]

  WATCH PLANE — Flask dashboard :5000 (REST + JSON files, never packets)
```

## 4. Workflow 1 — a transfer, click to done

1. Browser POSTs `/api/start-transfer` → Flask validates, rejects if busy
   (409), writes `transfer_request.json` with a unique `request_id`.
2. The bridge (on h1, polling) consumes it, spawns `client.py` → TCP to h3
   (h4 on failover).
3. Client: `HELLO(filename, size)` → ready ACK → per-chunk `DATA` + CRC32,
   one app-level `ACK` each → `[COMPLETE]` tally → exit 0.
4. Server verifies each CRC before writing; drops mismatches silently (no ACK).
5. Bridge writes `transfer_result.json` (same `request_id`); dashboard polls
   progress (bar %), chunks, throughput, and announces done/failed with
   duration. The reassembled file appears under Stored Files.

```
 h1 (client)                              h3 (server)
     | ---- HELLO{filename, size} -----------> |
     | <---------- ACK(0) ready -------------- |
     | ---- DATA[0] + CRC32 -----------------> |
     | <---------- ACK(0) -------------------- |
     |   ... one DATA/ACK round-trip per chunk ...  |
     | ---- DATA[N] + CRC32 -----------------> |
     | <---------- ACK(N), file complete ----- |
   [COMPLETE] tally printed, client exits 0 → bridge reports done
```

## 5. Workflow 2 — link dies mid-transfer, run survives

1. Mininet takes s1–s2 down (dashboard button or CLI) → switches emit
   port-status events.
2. Ryu: dead edge pruned from its map (discovery lags, so the event carries
   the amputation), Dijkstra recomputed, stale flows deleted, new flows
   installed — seconds.
3. Switches unblock the alternate path via STP (~tens of seconds).
4. Meanwhile the client **stalls, not dies**: 60 s ACK window (vs 10 s to
   connect) lets TCP retransmit through the blackhole; traffic resumes on the
   healed path, same run, zero rework.
5. Dashboard logs `[LINK]` (amber), then `[RECOVERY] Traffic flowing again
   Xs after link down` (green headline = the reroute time), then `[DONE]` with
   totals. Recovery Time in Metrics finally shows a number.

```
 BEFORE (healthy)                    AFTER (s1-s2 down)
 h1 -> s1 ==> s2 -> h3               h1 -> s1 -> s3 -> s2 -> h3
       direct, 1 hop                       2 hops, flows reinstalled
                                     (actual alternate may be via s4 —
                                      confirmed in controller log)
```

## 6. The dashboard↔Mininet contract

No RPC exists between host and Mininet namespaces — only JSON files in the
shared mount: transfer request/result/progress/events/cancel, link
request/result (id-echoed confirmations), bridge lock + heartbeat. Request IDs
make every result settle only its own run. Details: `docs/codebase/`.

```
 Flask (host namespace)                  Mininet hosts
  -- transfer_request.json ----> bridge on h1 (polls, runs client)
  <-- transfer_result / progress / events --'
  -- transfer_cancel.json -----> (kills the running client)
  -- link_request.json --------> topology watcher (breaks/mends links)
  <-- link_result.json ---------'
  <-- bridge_alive.json --------' (heartbeat → Bridge: alive indicator)
```

## 7. Protocol essentials

21-byte header (`DEADBEEF` magic, type, index, total, CRC32, length) + ≤8 KB
payload. Four messages: `HELLO, DATA, ACK, RESUME`. Golden rule: **a TCP ACK
means the kernel got bytes; our app ACK means the chunk passed CRC *and is on
disk*** — only app ACKs drive resume and completion.

```
 +--------+------+-------+------------+----------+----------+---------+
 | magic  | type | index | total_chks | checksum | data_len | payload |
 | 4 B    | 1 B  | 4 B   | 4 B        | 4 B      | 4 B      | <=8 KB  |
 +--------+------+-------+------------+----------+----------+---------+
 |<-------------- 21-byte header --------------->|
 magic = 0xDEADBEEF (garbage rejected) · type = DATA/ACK/RESUME/HELLO
```

## 8. Numbers that matter

- 8 KB chunks → 1 MB = 128 frames; 1.1 GB ≈ 144,000 frames
- Timeouts: 10 s connect (fail fast), 60 s mid-transfer ACK wait (outlast STP),
  300 s bridge deadline
- Measured (small files, reported): 1.3 s recovery (0.3 detect + 0.2 recompute
  + 0.8 re-converge); ~842 vs 850 KB/s dynamic vs static healthy
- Big-file reality: ~13 MB/s observed → minutes per GB; bar tracks chunk/total live

## 9. Honest limitations (say these out loud)

- STP reconvergence (~30–60 s) gates every recovery; rapid link flapping keeps
  it reconverging — kill once, leave it down.
- A blackhole longer than 60 s still fails the run (re-click on healed net).
- CRC detects accidents, not attackers; no TLS/auth; failover, not replication.
- Static baseline outcome is binary (fails) — no fair speed comparison exists.
- Controller↔app interact only through the network; no direct link.

## 10. Repo map & where to go deeper

```
client/ server/ common/      data plane (sockets + protocol)
controller/                  SDN brain (Ryu app)
mininet/                     topology + h1 bridge + link switch
frontend/                    Flask API + dashboard UI
tests/                       70+ tests (incl. 12 s blackhole survival, end-to-end)
shared_files/ stored_files/  live side-channel + received files (git-ignored)
docs/codebase/               per-file guides + viva map ← start here to learn the code
docs/d1-presentation-script.md  3-speaker demo script + Q&A bank
docs/runbook/MASTER.md       operational runbook (commands, processes, principles)
docs/architecture.md results.md test-cases.md   design record, numbers, test plan
```
