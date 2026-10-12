# D1 Demo Script — Distributed Backup System (3 speakers, in order)

Timing: ~10–12 minutes + questions. The 1.1 GB demo file runs for minutes, so
it transfers *behind* the closing slides instead of blocking them — the
choreography below says exactly when to start it, when to kill the link, and
what to do if the run fails on the blackhole (planned fallback, not panic).
Two rules for all three of you:
1. Say **"reported"** where the slides say reported, **"to be verified"** where
   they say pending/unverified. Never upgrade a maybe into a done.
2. Every short form is expanded the first time you say it — if you forget the
   expansion mid-demo, just read it off this script. Nobody will mind.

## Who says what (slides in order, 5 each)

- **Arush** — slides 1–5 (open, architecture, problems, TCP sockets, socket-flow picture)
- **Aryan** — slides 6–10 (protocol, topology picture, Mininet+SDN, reroute, integration)
- **Anubhav** — slides 11–15 (scenario, live demo, performance, tests, close)

## Rehearsal checklist (do once before demo day)

1. Create the file and record its exact size: `ls -l test_1gb.bin` — chunk
   count is file bytes ÷ 8192, roughly 144,000 for 1.1 GB. Read the real total
   off the dashboard ("Chunks: X / Y") and use *that* number on stage.
2. Time one clean transfer with no failure — that is your budget anchor
   (expect a couple of minutes at ~13 MB/s).
3. Time one kill-mid-transfer run — confirm the *same* run survives the
   blackhole and lands (no restart), so the fallback below stays unused.

---

## ARUSH — Slide 1: Title

> Good morning. We are Arush, Aryan and Anubhav, and our Computer Networks
> mini project is a **Distributed Backup System using TCP — Transmission
> Control Protocol — Socket Programming and SDN — Software-Defined
> Networking**. In one line: we back up files reliably over a small network
> whose paths are controlled by software, not fixed wiring. Our code is public
> at the GitHub link on screen.

## ARUSH — Slide 2: Overall System Architecture

> The system has two halves — this split runs through the whole project. On the
> **data plane** — the part that carries file data — clients h1 and h2 open TCP
> connections to backup servers h3 and h4 on port 9000, and file chunks flow
> there. On the **control plane** — the part that decides *where* data goes —
> one Ryu controller program talks OpenFlow version 1.3, which is just the
> language controllers use to program switches, to all four switches. The Flask
> dashboard on port 5000 only *watches* through REST — REST means normal web
> requests like your browser makes — it never touches a single file packet.
> The flow is: client connects, DATA goes out, ACKs — acknowledgements, meaning
> "got it" messages — come back, and Ryu learns the network and reacts when
> ports go down.

## ARUSH — Slide 3: Main Problems

> Three things go wrong in backup systems, and each maps to one thing we built.
> One: bits flip while travelling, so every 8-kilobyte chunk carries a CRC —
> Cyclic Redundancy Check, a small error-detecting code — and the server
> verifies and acknowledges each chunk. Two: connections drop mid-file, so
> instead of restarting we RESUME — continue — from the last acknowledged
> chunk, and both sides track progress. Three: network links die and traffic
> goes nowhere, so the controller recomputes shortest paths when a port-down
> event arrives and pushes new flow rules — new instruction sheets — to the
> switches.

## ARUSH — Slide 4: TCP Socket Programming

> The socket story in one walk: the client calls `connect`, the server has
> already done `bind`, `listen`, `accept` — that handshake is the famous 3-way
> handshake that opens a TCP connection. Then our own conversation on top:
> HELLO with filename and size, a ready ACK — acknowledgement — then DATA
> chunk *i* plus its CRC code, ACK *i*, repeated. On failure we send RESUME
> with the last ACK number and continue from *i+1*. `close` at the end is the
> FIN exchange — FIN is the finish flag that closes a connection.
> The one line to remember: **a TCP ACK is not our app ACK**. TCP's ACK only
> means the operating system's kernel received the bytes. Our `0x02` ACK means
> the chunk passed its CRC check **and was written to disk** — and only that
> ACK moves RESUME forward.

## ARUSH — Slide 5: Socket-flow picture

> Follow the arrows top to bottom: connect, HELLO, ready, then the chunk loop
> — DATA one way, ACK the other — and clean close at the bottom. The side
> branch is the interesting one: when the connection drops, we do *not* go
> back to the top; RESUME jumps back in mid-loop at the first missing chunk.
> That is the data half of the project — over to Aryan for the protocol and
> the network half.

## ARYAN — Slide 6: Protocol and Integrity

> Every frame — every single message — is a 21-byte header plus up to 8 KB of
> file data: a magic number `DEADBEEF` so garbage gets rejected, then type,
> index, total count, CRC code, and length. Four message types: DATA, ACK,
> RESUME, HELLO. The math on screen uses a 1 MB example — exactly 128 chunks
> of 8 KB. Our live demo scales the identical loop to a 1.1 GB file: roughly a
> hundred and forty-four thousand chunks — same frames, just more of them.
> The server recomputes the CRC, writes the chunk, and ACKs — every chunk, no
> exceptions. Two honest limits, both on the slide: CRC catches accidents, not
> attackers — no TLS — Transport Layer Security, meaning encryption — and no
> login security yet. And resume survives restarts because the server's folder
> is bind-mounted — that means shared — into our repo, so half-written files
> are still there after a reboot.

## ARYAN — Slide 7: Topology picture

> This is our actual Mininet network — Mininet is a tool that fakes a whole
> network inside one computer. Four OVS switches — OVS is Open vSwitch, a
> software version of a network switch — wired in a full mesh, meaning every
> switch connects to every other switch: six links. h1 and h2 are clients, h3
> and h4 are servers. The command that proves it works is `pingall` — every
> host pings every other host. The mesh shape is deliberate: with only one
> path, a dead link ends the story; with six links, the controller always has
> somewhere else to send traffic.

## ARYAN — Slide 8: Mininet + SDN Controller

> Two files do all of this. `topology.py` builds the virtual network, connects
> it to the controller using OpenFlow 1.3, and shares the backup folders so
> files survive outside Mininet. `sdn_controller.py` — the SDN brain — learns
> the network using LLDP — Link Layer Discovery Protocol, which is just
> switches introducing themselves to the controller — computes Dijkstra
> shortest paths — Dijkstra is a classic shortest-path algorithm — installs
> the flow rules, handles port-up and port-down events, and checks flow
> statistics every 5 seconds. The four commands on screen are the whole
> startup: start the network, start the controller, `pingall`, and
> `dump-flows` — which prints a switch's instruction sheet so you can see the
> rules with your own eyes.

## ARYAN — Slide 9: Dynamic Routing and Failure Recovery

> Left: all links up, h1 reaches h3 through switches s1–s2. Right: the s1–s2
> link dies — the cross on the diagram. The chain is five steps: the port goes
> down, a PortStatus alert — "a port changed state" message — reaches Ryu, the
> dead link is erased from the controller's map, Dijkstra recomputes the
> shortest path, and new flow rules go to the switches. One honesty note, also
> on the slide: the alternate path shown is illustrative — real traffic may go
> via s3 or s4 instead — and we always confirm the actual path in the
> controller log. A static, hand-written flow table could never recompute like
> this; that difference is the whole point of doing it with SDN —
> Software-Defined Networking.

## ARYAN — Slide 10: Socket–SDN Integration

> The application layer — the backup program — splits files, connects,
> verifies, stores, and on error resumes or switches to the backup server h4.
> The network layer — controller plus switches — installs paths, forwards by
> the instruction sheet, and reinstalls everything when a PortStatus alert
> arrives. And here is the exam answer, word for word: **the controller never
> sees a single file chunk — there is no direct wire between controller and
> app; they interact only through the network itself.** Recovery comes in three
> independent layers, and here is how they divide the job tonight: Ryu reroutes
> around the dead link, TCP — Transmission Control Protocol — retransmits
> through the blackhole, and crucially the client *waits out* the stall — a
> full minute of patience for ACKs instead of the usual ten seconds — so the
> *same run* continues on the healed path with zero rework. Over to Anubhav —
> who starts our 1.1 GB transfer live, right now, so it runs behind the rest
> of the talk.

## ANUBHAV — Slide 11: Challenging Scenario (+ transfer starts here)

> Our challenge test: a 1.1 GB file — roughly a hundred and forty-four
> thousand 8 KB chunks, exact total on the dashboard — sent from h1 to h3 over
> s1–s2. I have just pressed Start Transfer, so watch the bar and the chunk
> count climb while I talk. Partway through, we kill s1–s2, from the dashboard
> button or the Mininet command line. The chain fires: port alert, recompute,
> new flows — and this run does *not* restart; it stalls, TCP retransmits, and
> continues where it stopped. The 1.3-second recovery on screen comes from our
> small-file runs; at this scale, watch the bar, not the stopwatch — the
> reroute takes the same time regardless of file size. And the careful part —
> four **separate** claims: connectivity restored is *reported*; TCP keeping
> the connection alive is *unverified* on the old 10-second timeout, and
> *exactly what this demo tests* on the new 60-second one; the app-level RESUME
> path is untouched tonight — survival comes from patience, not resume; file
> integrity is reported, and I re-check it with `sha256sum` — a fingerprint
> command proving two files are byte-identical — when the run lands.

## ANUBHAV — Slide 12: LIVE DEMO (Anubhav drives, Aryan narrates the network)

> The transfer is already running behind us. Three beats from here:
1. `mininet> pingall` — all reachable. (Aryan: "Baseline is green.")
2. **Trigger Link Failure**, about a third of the way into the bar — (Aryan,
   watching the controller terminal: "Port-down alert arrived — recomputing —
   new flows installed.") Then `sh ovs-ofctl -O OpenFlow13 dump-flows s1` —
   the new path, on screen — and `pingall` again once it settles: clean on the
   surviving mesh. The bar will look stuck during the blackhole — say so:
   "no ACKs moving, client waiting out its 60-second window."
3. When the bar lands: transfer completes, every chunk received →
   `sha256sum` the original and the stored copy: identical fingerprints.
>
> Planned fallback, only if the run fails on a longer-than-expected blackhole:
> say it plainly — "the stall outran even the 60-second window, so this run is
> lost — but the network layer won, `pingall` is clean on the new path" — then
> re-click Start on the healed network and let it run behind the closing
> slides; the hash check becomes your ending instead of your middle. If
> anything else stalls: narrate what you see ("holding at N chunks while the
> controller recomputes") — a narrated hiccup scores more than a hidden one.

## ANUBHAV — Slide 13: Performance and Baseline

> The recovery clock splits into three stages: 0.3 to *notice* the dead link,
> 0.2 to *recompute* paths, 0.8 until traffic actually flows — the physical
> network settling down is the slow part, not our code, and it costs the same
> whether the file is 1 MB or 1.1 GB. Against the static, non-smart baseline:
> same speed when healthy, 842 versus 850 kilobytes per second — those
> baselines are from the small-file runs; our big run's live numbers are on
> the dashboard behind me. After a failure, dynamic recovers while static
> fails outright with every chunk lost. Stated honestly: the static result is
> pass-or-fail, so a fair speed comparison is impossible, and loss percentages
> are still to be measured.

## ANUBHAV — Slide 14: Testing and Results

> Walk the table row by row, and read the status column *exactly as written*.
> Our stall tests are the newest row in spirit: a 12-second ACK blackhole —
> past the old 10-second death line — survived by the same run, failed fast on
> a short timeout. That column is our work plan for phase 2, not a weakness:
> every test case already defines its starting condition and its expected
> result before we run it. (If the big transfer is still flowing, gesture at
> it — "still climbing, zero failed chunks" — it is the best background visual
> on stage.)

## ANUBHAV — Slide 15: Conclusion + close

> What we demonstrated: TCP sockets with a custom message protocol, CRC codes
> with per-chunk ACKs and resume, Mininet plus OpenFlow 1.3 plus the Ryu
> controller, and Dijkstra rerouting on link failure — strongest result, fast
> recovery with every chunk received, reported. Limitations, plainly: app and
> controller are not directly connected, controller tests are at the map level
> only, few runs so far, CRC only with no security, static baseline speed
> unmeasured, and failover to one server rather than true copying to both.
> Each limitation maps to a future item: bigger test sweeps, SHA — Secure Hash
> Algorithm — fingerprinting plus TLS encryption, a REST — web-request — hook
> so the app can talk to the controller, choosing paths by measured bandwidth,
> and storing every chunk on both servers. Code is on GitHub — thank you, and
> we welcome questions. (If the transfer just landed: end on the hash match —
> nothing closes a demo like two identical fingerprints.)

---

## Likely teacher questions (one-line answers, expansions included)

- **Why TCP — Transmission Control Protocol — and not UDP?** — TCP gives
  ordered, reliable bytes plus free retransmission; our ACKs add
  storage-level certainty on top. (Arush)
- **TCP ACK versus your ACK?** — Kernel receipt versus CRC-checked-and-stored;
  only ours moves resume forward. (Arush)
- **Why 8 KB chunks?** — Small enough to resume cheaply, big enough to avoid
  drowning in per-chunk ACKs; 1 MB ÷ 8 KB = 128 frames, and the demo file is
  the same frames × ~144,000. (Aryan)
- **CRC — Cyclic Redundancy Check — versus SHA?** — CRC is fast for per-chunk
  checks; it detects corruption, not attackers (a listed limitation). (Aryan)
- **Server h3 dies mid-transfer?** — The client tries servers in order, so h4
  takes over (failover). (Aryan)
- **How do app and controller interact?** — They don't, directly — only
  through the network itself (slide 10). (Aryan)
- **The controller program itself dies?** — Switches keep their last
  instructions; new events go unhandled — an honest gap, listed as future work.
- **Static baseline numbers?** — Pass-or-fail outcome, so no fair speed
  comparison — stated on slide 13. (Anubhav)
- **Prove the reroute path?** — Controller log plus `dump-flows`, live in
  the demo. (Anubhav)
- **Why did the big transfer take minutes?** — One ACK round-trip per 8 KB
  chunk, ~144,000 of them — correct but slow by design; pipelining is future
  work. (Anubhav)
- **Why didn't the run die when the link died?** — Split timeouts: 10 s to
  connect, 60 s to wait for ACKs mid-transfer — the run stalls while TCP
  retransmits, then continues on the healed path. Tested with a 12 s
  ACK blackhole. (Anubhav)
