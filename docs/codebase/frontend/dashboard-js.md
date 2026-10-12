# `frontend/static/dashboard.js` — dashboard UI logic

Polling-first frontend (REST every 2 s); a socket.io client is optional and
probed for (`typeof io !== 'undefined'`) — polling is the channel that always
works, which is why the dead `/static/socket.io.min.js` reference was removed
rather than vendored.

## State & setup

- `socket` (nullable), `throughputChart` (Chart.js or null — every chart call
  is guarded), `selectedFile`, `lastResultSeen`, `transferRunning`,
  `startInFlight` (double-click guard while the POST is in flight).

## Handlers

- **Start** — needs a selected file; ignores double-clicks and clicks while a
  run is live; disables the button, renders an **optimistic "Transferring…"**
  instantly (not on the next tick), POSTs, announces queued/rejected/failed,
  then `await pollOnce()` to reconcile with the server immediately.
- **Stop** — POSTs cancel; the bridge kills the client (`stopped` result).
- **Link down/up** — POSTs `s1↔s2`, announces the outcome.
- **Refresh buttons** — reload file lists.

## Rendering

- `updateProgress(state)` — defensive (`state || {}`, numeric coercion so one
  bad field can't freeze the panel): running → `Transferring f: p%` + striped
  bar; done/failed/stopped → **bar reset to idle**, history kept in the text
  line (`Idle — last transfer … completed/failed at …`); plus chunks,
  throughput, and chart history push.
- `updateLinkStatus(sw1, sw2, status)` — toggles the SVG link's `down` class.
- `loadFiles()` — stored-files panel from `/api/files`. `loadBrowse()` —
  picker with click-to-select. `formatSize()` — B/KB/MB.
- `pollOnce()` (every 2 s + once on load + after Start): recovery metric,
  transfer-result notice keyed on **`request_id` + status** (filename fallback)
  with `loadFiles()` on done, then status → progress/running/button/events/
  bridge-indicator updates.

## Contract with `dashboard.html` (element IDs)

`throughput-chart`, `progress-bar`, `progress-text`, `chunks-value`,
`throughput-value`, `recovery-value`, `bridge-value`, `files-list`,
`browse-list`, `btn-start|stop|link-down|link-up|refresh-files|refresh-browse`,
`event-log`, `link-s1-s2` (and siblings). Rename an ID in one file without the
other and that panel silently stops updating — there is no test covering the
HTML↔JS seam (JS is syntax-checked with `node --check`, not unit-tested).
Note: `.done/.failed/.stopped` CSS classes exist but are currently unused —
terminal states reset the bar to `''` by design.
