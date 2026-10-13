# CN-Mini Master Document — Distributed Backup System

*One doc, end to end: what the project is, what every part is and does, and
how a file gets from a click to safe storage across a self-healing network.
Systems-level — no function signatures; for per-file detail see
`docs/codebase/`.*

## 1. What this is (and what problem it solves)

A **client** is a machine that *sends* files for safekeeping. A **backup
server** is a machine that *receives and stores* them. The problem: networks
corrupt data, connections drop halfway, and links die — any of which can
silently destroy a backup. This project builds a small backup service where:

- files travel over **TCP** (Transmission Control Protocol — the reliable,
  ordered byte-stream protocol, as opposed to fire-and-forget UDP),
- every file is split into **chunks**, each carrying its own integrity
  fingerprint, so corruption is caught per chunk, not per file,
- an interrupted transfer **resumes** where it stopped instead of restarting,
- the network itself is **software-controlled (SDN)**: when a link dies, the
  controller computes a new path and traffic keeps flowing,
- a **dashboard** shows all of it live: progress, speed, events, stored files.

**One line:** TCP file backup with chunking, checksums and resume, running on
a Mininet mesh that heals itself via a Ryu controller, visualized on a Flask
dashboard.

## 2. Frameworks — what each one is and what it does here

- **Python 3 — the programming language everything is written in.** Sockets,
  controller app, dashboard, tests. Chosen because the course stack and all
  three frameworks (Mininet, Ryu, Flask) speak Python, so the whole system is
  one language with no glue code.
- **Mininet — a tool that fakes an entire network inside one computer.** It
  creates virtual hosts (h1–h4, each with its own IP and network namespace, so
  they behave like separate machines) and virtual switches (s1–s4) connected by
  virtual cables, all on your Ubuntu VM. It exists so the topology is
  reproducible without buying hardware.
- **Open vSwitch (OVS) — the actual switch software Mininet uses.** These are
  real, production-grade software switches that understand **OpenFlow 1.3**,
  the standard language a controller uses to program switch behavior
  ("send packets matching X out of port Y"). Using OVS is what makes this an
  honest SDN testbed rather than a simulation.
- **Ryu — the SDN controller, a Python program.** A controller is the network's
  brain: switches ask it what to do with traffic they don't recognize, and it
  answers with flow rules. Ours learns the topology, computes shortest paths,
  and — the demo moment — recomputes them when a link dies. Chosen for being
  lightweight, Python-native, and event-driven.
- **Flask + vanilla JavaScript — the dashboard.** Flask is a minimal Python web
  framework serving a REST API (normal web requests: start/stop transfer, read
  status); the page itself is plain JS polling every 2 seconds, an SVG topology
  drawing, and a Chart.js throughput graph. No build step, no dependencies to
  install — it runs anywhere a browser exists.
- **zlib CRC32 — the integrity fingerprint.** CRC (Cyclic Redundancy Check) is
  a fast error-detecting code: feed it bytes, get a 32-bit number; one flipped
  bit gives a different number. Computed per 8 KB chunk — fast enough for
  ~144,000 chunks per gigabyte, and it catches random corruption (it is *not*
  security: a deliberate forger can fake it).
- **pytest — the test runner.** 70+ tests cover the protocol, transfers,
  resume, controller rerouting, dashboard races, and a full local end-to-end
  chain. Every bug class met during the build has a test pinning it.

No Docker, no cloud, no paid services. Three terminals on an Ubuntu VM is the
entire deployment.

## 3. System map — the parts and how they connect

**Hosts** (virtual machines-with-an-IP inside Mininet):
`h1 = 10.0.0.1` and `h2 = 10.0.0.2` are *clients* (they send files);
`h3 = 10.0.0.3` and `h4 = 10.0.0.4` are *backup servers* (they receive and
store). h1 also runs the **bridge**, a helper program that acts on the
dashboard's behalf *inside* the virtual network.

**Switches** `s1–s4` forward packets between hosts according to flow rules.
They talk to the controller on port 6633 (OpenFlow channel) and carry backup
traffic on TCP port 9000. **STP** (Spanning Tree Protocol) is enabled on them:
because the mesh contains loops, broadcast traffic would otherwise circle
forever and melt the network (a real outage we hit and fixed); STP prunes
broadcast to a loop-free tree.

**The three terminals**, each one process with one job:

```
Terminal 1: sudo python3 mininet/topology.py   → builds the net, bind-mounts
                                                 folders, launches bridge (h1)
                                                 + both servers (h3, h4)
Terminal 2: ryu-manager controller/sdn_controller.py → learns MAC addresses,
                                                 installs Dijkstra shortest-path
                                                 flows, reroutes on port events
Terminal 3: python frontend/app.py             → dashboard at localhost:5000
```

**Bind mounts** are shared folders between the real machine and the virtual
hosts (a *mount namespace* trick): `stored_files/h3|h4` appears inside h3/h4
as `/tmp/backup`, so received files land in the repo where the dashboard lists
them; `shared_files/` appears on *all* hosts as `/tmp/shared`, carrying the
JSON side-channel (requests, results, heartbeats) both ways. Without mounts,
the dashboard (host namespace) and the hosts (Mininet namespaces) could not
exchange a single byte.

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

### The three planes (who carries what)

```
  DATA PLANE — file bytes, TCP :9000
  [ h1 / h2 clients ] <=== chunks + CRC32 / ACKs ===> [ h3 / h4 servers ]

  CONTROL PLANE — decisions, OpenFlow 1.3
  [ Ryu controller ] ---- flow rules ----> [ s1 s2 s3 s4 ]

  WATCH PLANE — Flask dashboard :5000 (REST + JSON files, never packets)
```

*Data plane* = the file bytes moving. *Control plane* = the decisions about
*where* bytes go (flow rules pushed into switches). *Watch plane* = humans
looking at it all. The planes never mix: the controller never sees file
chunks, the dashboard never touches packets.

## 4. Workflow 1 — a transfer, click to done

1. **Click → request.** The browser POSTs `/api/start-transfer` with a
   filename. Flask checks the name is safe (no path tricks), the file exists,
   and nothing is already running (a second request would overwrite the first
   mid-flight, so it gets a `409 already running` instead). It writes
   `transfer_request.json` carrying a unique `request_id` — an ID minted per
   click so the eventual result can only ever settle *its own* run, never a
   stale or duplicate one.
2. **Bridge picks it up.** The bridge program on h1 polls the shared folder,
   consumes the request, marks progress "running", and spawns `client.py` as a
   subprocess aimed at h3 (h4 if h3 refuses — that ordered try-each is the
   server failover).
3. **The conversation.** Client sends `HELLO` (filename + size) → server ACKs
   "ready" → then one `DATA` chunk + CRC32 at a time, each answered by an
   app-level `ACK`. After the last chunk the client prints a `[COMPLETE]`
   tally (how many chunks the server acknowledged) and exits 0. Done requires
   *both* the tally and exit 0 — exit code alone once certified failures as
   successes.
4. **Server side.** Each chunk's CRC is recomputed *before* writing; a mismatch
   is dropped silently (no ACK), which the client reads as loss — corruption
   and loss share one code path. A complete file is flushed to `/tmp/backup`,
   i.e. into `stored_files/`.
5. **Done, visibly.** The bridge writes `transfer_result.json` (same
   `request_id`); the dashboard's polls pick up the bar %, chunk counts,
   throughput, and announce done/failed *with duration*. The Stop button drops
   a cancel file the bridge honors by killing the client.

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

1. **Break it.** Mininet takes s1–s2 down (dashboard button or CLI). The
   switches on both ends emit **port-status events** — "a port changed state"
   messages — to Ryu. (This is why the dashboard breaks the *real* link
   instead of pretending: only a real event exercises the real chain.)
2. **Reroute (seconds).** Ryu prunes the dead `(switch, port)` from its map —
   explicitly, because LLDP discovery still lists the corpse for seconds —
   recomputes **Dijkstra shortest paths** (the classic algorithm: cheapest
   hop-count route between every host pair), deletes the stale flow rules, and
   installs new ones pointing around the failure.
3. **Reconverge (tens of seconds).** The switches unblock the alternate path
   via STP. This physical settling is the slow pole of every recovery — no
   code can skip it.
4. **Stall, don't die.** The client stops getting ACKs but keeps waiting: the
   mid-transfer ACK window is 60 s (vs 10 s just to connect), so TCP
   retransmission rides out the blackhole and traffic resumes on the healed
   path — *same run, zero rework*. The byte stream resumes exactly; the server
   just sees delayed frames.
5. **Proof on screen.** Dashboard logs `[LINK]` (amber) when Mininet confirms,
   then a green headline `[RECOVERY] Traffic flowing again Xs after link
   down` — measured from down-confirm to the next chunk advance, i.e. timed
   where traffic is actually observable — then `[DONE]` with totals. Recovery
   Time in Metrics finally shows a number.

```
 BEFORE (healthy)                    AFTER (s1-s2 down)
 h1 -> s1 ==> s2 -> h3               h1 -> s1 -> s3 -> s2 -> h3
       direct, 1 hop                       2 hops, flows reinstalled
                                     (actual alternate may be via s4 —
                                      confirmed in controller log)
```

## 6. The dashboard↔Mininet contract (why files, not API calls)

Host processes and Mininet hosts live in different network namespaces with no
RPC between them — but they share a filesystem through the bind mount. So the
entire contract is **plain JSON files**, each with one job:

- `transfer_request.json` — dashboard → bridge: `{filename, request_id}`.
  The bridge deletes it on pickup (consumed exactly once).
- `transfer_result.json` — bridge → dashboard: `{filename, request_id,
  status, error?}`. Terminal states: `done / failed / stopped`.
- `transfer_progress.json` — bridge → dashboard, rewritten per chunk:
  `{filename, request_id, running, chunk, total, throughput}`. The bar reads
  `chunk/total`; `running: true` means "bridge busy".
- `transfer_events.json` — bridge → dashboard: append-only log tail the
  dashboard ingests by index.
- `transfer_cancel.json` — dashboard → bridge: mere presence means "kill the
  running client now" (the Stop button).
- `link_request.json` / `link_result.json` — dashboard → topology watcher and
  back. Results echo the request's ID so a stale confirmation can't pose as a
  fresh one; no echo within 5 s → the dashboard reports "unconfirmed" instead
  of lying.
- `bridge.lock` — singleton: freshness means a live bridge; lets the second
  bridge exit instead of racing (duplicate bridges = duplicate transfers).
- `bridge_alive.json` — heartbeat (pid, timestamps) behind the `Bridge: alive`
  indicator. It ticks during transfers too, or long runs would read "dead".

```
 Flask (host namespace)                  Mininet hosts
  -- transfer_request.json ----> bridge on h1 (polls, runs client)
  <-- transfer_result / progress / events --'
  -- transfer_cancel.json -----> (kills the running client)
  -- link_request.json --------> topology watcher (breaks/mends links)
  <-- link_result.json ---------'
  <-- bridge_alive.json --------' (heartbeat → Bridge: alive indicator)
```

## 7. Protocol essentials (what each byte is for)

A 21-byte header plus up to 8 KB of file data. Every field earns its place:
`magic` (`0xDEADBEEF`) rejects garbage/old versions at a glance; `type` says
which of the four messages this is; `index`/`total` locate the chunk in the
file (and drive the progress bar); `checksum` is the CRC32 of *this chunk's*
payload; `data_len` tells the receiver exactly where the frame ends (TCP is a
stream — without lengths, messages blur together).

Four messages, each with one job: `HELLO` announces a transfer (name, size,
resume point); `DATA` carries one chunk; `ACK` confirms one chunk index (or
readiness); `RESUME` restarts from the last good chunk after an interruption.

Golden rule: **a TCP ACK means the kernel got bytes; our app ACK means the
chunk passed CRC *and is on disk*** — only app ACKs drive resume and
completion. Confusing the two is the classic viva trap.

```
 +--------+------+-------+------------+----------+----------+---------+
 | magic  | type | index | total_chks | checksum | data_len | payload |
 | 4 B    | 1 B  | 4 B   | 4 B        | 4 B      | 4 B      | <=8 KB  |
 +--------+------+-------+------------+----------+----------+---------+
 |<-------------- 21-byte header --------------->|
 magic = 0xDEADBEEF (garbage rejected) · type = DATA/ACK/RESUME/HELLO
```

## 8. Numbers that matter (and why those values)

- **8 KB chunks** — small enough that resuming wastes little, big enough that
  per-chunk ACKs don't drown the transfer. 1 MB = 128 frames; 1.1 GB ≈
  144,000 frames (read the exact total off the dashboard's chunk counter).
- **Timeouts** — 10 s to *connect* (a truly dead server should fail fast),
  60 s waiting for *ACKs mid-transfer* (must outlast STP reconvergence or
  every failure kills the run), 300 s bridge deadline (must cover a big file
  *plus* one stall).
- **Measured, small files (reported)** — 1.3 s recovery = 0.3 detect + 0.2
  recompute + 0.8 re-converge. Healthy speed ≈ 842 vs 850 KB/s dynamic vs
  static (effectively tied — the win is surviving failure, not speed).
- **Big-file reality** — ~13 MB/s observed → minutes per GB; the bar tracks
  `chunk/total` live, and the graph keeps the throughput history.

## 9. Honest limitations (say these out loud)

- **STP reconvergence (~30–60 s) gates every recovery** — controller logic is
  seconds, physics is tens of seconds. Rapid link flapping keeps STP
  reconverging forever: kill once, leave it down.
- A blackhole longer than 60 s still fails the run (re-click on the healed
  network — the scripted fallback, not a surprise).
- **CRC detects accidents, not attackers**; no TLS, no auth; failover to one
  server, not replication to both.
- Static baseline outcome is binary (it just fails) — no fair speed comparison
  exists, and we say so instead of inventing one.
- Controller and app interact **only through the network** — there is no
  direct link, by design (separation of planes).

## 10. Repo map & where to go deeper

```
client/ server/ common/      data plane — sockets + protocol (sender, receiver, shared wire format)
controller/                  SDN brain — the Ryu app (topology, paths, reroute)
mininet/                     topology (mesh + mounts + launches) · bridge (h1 agent) · link switch
frontend/                    Flask REST API + dashboard UI (app.py, dashboard.js)
tests/                       70+ tests — protocol, transfer, resume, 12 s blackhole survival,
                             controller reroute, dashboard races, full local end-to-end
shared_files/ stored_files/  live side-channel + received files (git-ignored, created at runtime)
docs/codebase/               per-file guides + viva map ← start here to learn the code
docs/d1-presentation-script.md  3-speaker demo script (Arush 1–5, Aryan 6–10, Anubhav 11–15) + Q&A bank
docs/runbook/MASTER.md       operational runbook — commands, processes, principles
docs/architecture.md · results.md · test-cases.md   design record, measured numbers, test plan
```
