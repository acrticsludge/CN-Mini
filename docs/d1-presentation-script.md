# D1 Demo Script — Distributed Backup System (3 speakers)

Timing: ~10 minutes + questions. Rule for everyone: say **"reported"** where the
slides say reported, and **"to be verified"** where they say pending/unverified.
Never upgrade a maybe into a done — the teacher will probe exactly those lines.

## Suggested split

- **Speaker 1** — slides 1–3 (open, architecture, problems) + slides 14–15 (tests, close)
- **Speaker 2** — slides 4–6 (TCP sockets, sequence picture, protocol) + slides 11–12 (scenario + live demo)
- **Speaker 3** — slides 7–10 (topology picture, Mininet+SDN, reroute, integration) + slide 13 (performance)

---

## SPEAKER 1 — Slide 1: Title

> Good morning. We are [Anubhav, Arush, Aryan], and our Computer Networks
> mini project is a **Distributed Backup System using TCP Socket Programming
> and SDN**. In one line: we back up files reliably over a small network
> whose paths are controlled by software, not fixed wiring. Our code is public
> at the GitHub link on screen.

## SPEAKER 1 — Slide 2: Overall System Architecture

> The system has two planes — please notice the separation, it runs through
> the whole project. On the **data plane**, clients h1 and h2 open TCP
> connections to backup servers h3 and h4 on port 9000, and file chunks flow
> there. On the **control plane**, one Ryu controller speaks OpenFlow 1.3 to
> all four switches and decides the paths. The Flask dashboard on port 5000
> only *watches* through REST — it never touches a single packet.
> The flow is: client connects, DATA goes out, ACKs come back, Ryu discovers
> the topology with LLDP and reacts to port events. Over to [Speaker 2] for
> why each piece exists.

## SPEAKER 1 — Slide 3: Main Problems

> Three things go wrong in backup systems, and each maps to one thing we built.
> One: bits flip in transit, so every 8 KB chunk carries a CRC32 and the server
> verifies and ACKs each chunk. Two: connections drop mid-file, so instead of
> restarting we RESUME from the last ACKed chunk — both sides track progress.
> Three: links die and traffic black-holes, so the controller recomputes
> Dijkstra paths when a port-down event arrives and pushes new flow rules.

## SPEAKER 2 — Slide 4: TCP Socket Programming

> The socket story in one walk: the client calls `connect`, the server has
> done `bind`, `listen`, `accept` — that is the 3-way handshake. Then our
> application conversation: HELLO with filename and size, ready ACK, then DATA
> chunk *i* plus CRC32, ACK *i*, repeated. On failure we send RESUME with the
> last ACK and continue from *i+1*. `close` is the FIN exchange.
> The one line to remember: **a TCP ACK is not an app ACK**. TCP's ACK only
> means the kernel got the bytes. Our `0x02` ACK means the chunk passed CRC32
> **and was written to disk** — and only that ACK moves RESUME forward.

## SPEAKER 2 — Slide 5: Socket-flow picture

> Follow the arrows top to bottom: connect, HELLO, ready, then the chunk loop
> — DATA one way, ACK the other — and clean close at the bottom. The side
> branch is the interesting one: when the connection drops, we do *not* go
> back to the top; RESUME re-enters mid-loop at the first missing chunk.

## SPEAKER 2 — Slide 6: Protocol and Integrity

> Every frame is a 21-byte header plus up to 8 KB of payload: magic `DEADBEEF`
> so garbage is rejected, then type, index, total, CRC32, and length. Four
> message types: DATA, ACK, RESUME, HELLO. The math that matters: a 1 MB file
> is exactly 128 chunks of 8 KB. The server recomputes the CRC, writes, and
> ACKs — per chunk, no exceptions. Two honest limits, both on the slide: CRC32
> catches accidents, not attackers — no TLS, no auth yet. And resume works
> because the server's disk is bind-mounted into our repo, so partial files
> survive restarts.

## SPEAKER 3 — Slide 7: Topology picture

> This is our actual Mininet network: four OVS switches in a full mesh — six
> inter-switch links — h1 and h2 as clients, h3 and h4 as servers. The command
> that proves it is `pingall`: all hosts reachable. The mesh shape is
> deliberate: with only one path, a link failure ends the story; with six
> links, the controller always has somewhere else to go.

## SPEAKER 3 — Slide 8: Mininet + SDN Controller

> Two files do all of this. `topology.py` builds the mesh, attaches the remote
> controller over OpenFlow 1.3, and bind-mounts the backup directories so files
> persist outside Mininet. `sdn_controller.py` discovers the topology with
> LLDP, computes Dijkstra shortest paths, installs the flow rules, handles
> port-status events, and polls flow stats every 5 seconds. The four commands
> on screen are the whole bring-up: network, controller, `pingall`, and
> `dump-flows` to see the installed rules with your own eyes.

## SPEAKER 3 — Slide 9: Dynamic Routing and Failure Recovery

> Left: all links up, h1 reaches h3 over s1–s2. Right: s1–s2 goes down — the
> cross on the diagram. The chain is five steps: port goes down, a PortStatus
> event reaches Ryu, the dead edge leaves the graph, Dijkstra recomputes, new
> flows go to the switches. One honesty note, also on the slide: the alternate
> path is illustrative — it may run via s3 or s4 — and we always confirm the
> real path in the controller log. A static flow table would simply not
> recompute; that is the whole point of doing it with SDN.

## SPEAKER 3 — Slide 10: Socket–SDN Integration

> The application layer chunks, connects, verifies, stores, and on error
> resumes or fails over to h4. The network layer installs paths, forwards by
> flow table, and reinstalls on PortStatus. And here is the exam answer: **the
> controller never sees a single chunk or RESUME — there is no direct
> controller-to-app link; they interact only through the network.** Recovery
> comes in three independent layers: Ryu reroutes, TCP retransmits, the app
> resumes or fails over. Any one of them can save a transfer the others lost.

## SPEAKER 2 — Slide 11: Challenging Scenario

> Our scenario: a 1 MB file, 128 chunks, h1 to h3 over s1–s2 — then we kill
> s1–s2 mid-transfer, from the dashboard button or the Mininet CLI. The chain
> fires: PortStatus, recompute, new flows. Reported outcome: traffic flowing
> again in 1.3 seconds, all 128 chunks received. And now the careful part —
> these are **four separate claims**: connectivity restored is reported at
> 1.3 s; TCP preserving the connection is *unverified* — the docs credit
> retransmission and we still have to confirm it in the client log; app RESUME
> being used is *unverified* for the same reason; integrity at 128/128 is
> reported and we re-verify it live with `sha256sum` in the demo.

## SPEAKER 2 — Slide 12: LIVE DEMO (Speaker 2 drives, Speaker 3 narrates network)

1. `mininet> pingall` — all reachable. (Speaker 3: "Baseline is green.")
2. Dashboard: pick the 1 MB file, **Start Transfer** — point at the progress bar.
3. Mid-transfer: **Trigger Link Failure** — (Speaker 3, watching controller
   terminal: "PortStatus arrived — recompute — new flows installed.")
4. Transfer completes 128/128 → `sha256sum` original vs stored copy: identical.
5. `sh ovs-ofctl -O OpenFlow13 dump-flows s1` — the new path, on screen.
>
> If anything stalls: say what you see ("progress held at N chunks while the
> controller recomputes") — narrating a hiccup scores more than hiding it.

## SPEAKER 3 — Slide 13: Performance and Baseline

> The 1.3 seconds split into three stages: 0.3 to detect, 0.2 to recompute,
> 0.8 until traffic flows — detection is cheap, the physical re-convergence is
> the long pole. Against the static baseline: same speed when healthy, 842
> versus 850 KB/s — but after a failure, dynamic recovers in 1.3 s while
> static fails outright with total chunk loss. Stated honestly: the static
> outcome is binary, so a fair rate comparison is impossible, and bigger files
> and loss percentages are still to be measured.

## SPEAKER 1 — Slide 14: Testing and Results

> Walk the table row by row, and say the status column out loud exactly as
> written — most unit rows are *pending runs*, the Mininet rows are
> *not documented yet*, and only the live reroute row is *reported, to
> re-verify*. That column is our work plan for D2, not a weakness: every test
> has its initial condition and expected result defined before we ran it.

## SPEAKER 1 — Slide 15: Conclusion + close

> What we demonstrated: TCP sockets with a custom framed protocol, CRC32 with
> per-chunk ACKs and resume, Mininet plus OpenFlow 1.3 plus Ryu, Dijkstra
> rerouting on failure — strongest result, 1.3-second recovery with 128 of 128
> chunks, reported. Limitations, plainly: app and controller are decoupled,
> SDN tests are graph-level, small file with few runs, CRC32 only with no
> security, static baseline unmeasured, failover rather than true replication.
> Each limitation maps to a future item: bigger sweeps, SHA plus TLS, a
> controller REST hook, bandwidth-aware weights, replicating to both servers.
> Code is on GitHub — thank you, and we welcome questions.

---

## Likely teacher questions (one-line answers)

- **Why TCP, not UDP?** — Ordered, reliable bytes plus free retransmission; our ACKs add storage-level certainty on top.
- **TCP ACK vs your ACK?** — Kernel receipt vs CRC-verified-and-stored; only ours drives resume.
- **Why 8 KB chunks?** — Resume granularity vs per-chunk ACK overhead; 1 MB ÷ 8 KB = 128 frames.
- **CRC32 vs SHA?** — Speed for per-chunk checks; detects corruption, not attackers (a listed limitation).
- **h3 dies mid-transfer?** — Client tries servers in order; h4 takes over.
- **How do app and controller interact?** — They don't, directly — only through the network (slide 10).
- **Controller itself dies?** — Switches keep last flows; new events unhandled — honest gap, future work.
- **Static baseline numbers?** — Binary outcome (fails), so no fair rate comparison — stated on slide 13.
- **Prove the reroute path?** — Controller log + `dump-flows`, in the demo.
