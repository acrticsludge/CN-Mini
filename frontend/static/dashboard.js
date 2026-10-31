/**
 * Dashboard frontend logic — WebSocket events, topology rendering,
 * transfer control, metrics charts, file listing.
 */

// Live updates via WebSocket when a client library is present; REST polling
// below is the primary channel and always runs.
let socket = null;
try { socket = (typeof io !== 'undefined') ? io() : null; } catch (e) { socket = null; }
const ctx = document.getElementById('throughput-chart').getContext('2d');

// Throughput chart
const throughputChart = (typeof Chart === 'undefined') ? null : new Chart(ctx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [{
            label: 'Throughput (KB/s)',
            data: [],
            borderColor: '#4ecca3',
            backgroundColor: 'rgba(78, 204, 163, 0.1)',
            fill: true,
            tension: 0.4,
        }]
    },
    options: {
        responsive: true,
        scales: {
            x: { display: false },
            y: { beginAtZero: true, ticks: { color: '#aaa' }, grid: { color: '#0f3460' } }
        },
        plugins: { legend: { labels: { color: '#eee' } } }
    }
});

// State
let throughputData = [];
let selectedFile = null;
let lastResultSeen = null;
let transferRunning = false;
let startInFlight = false;

// Socket events (only when the library loaded; otherwise the polling below covers updates)
if (socket) {
socket.on('connect', () => {
    addEvent('Connected to server');
});

socket.on('event', (data) => {
    addEvent(data.message);
});

socket.on('transfer_state', (state) => {
    updateProgress(state);
});

socket.on('link_event', (data) => {
    updateLinkStatus(data.sw1, data.sw2, data.status);
    addEvent(`Link ${data.sw1} <-> ${data.sw2}: ${data.status}`);
});
}

// Button handlers
document.getElementById('btn-start').addEventListener('click', async () => {
    if (!selectedFile) {
        addEvent('Pick a file from Shared Files first');
        return;
    }
    if (startInFlight) {
        return; // double click while the first POST is still going out
    }
    if (transferRunning) {
        addEvent('Transfer already running — wait for it or press Stop');
        return;
    }
    startInFlight = true;
    document.getElementById('btn-start').disabled = true;
    // Optimistic "Transferring…" right now — not on the next 2s tick — so the
    // click visibly takes. The sync below reconciles with the server instantly.
    transferRunning = true;
    updateProgress({ running: true, filename: selectedFile, progress: 0,
                     chunks_sent: 0, total_chunks: 0, throughput: 0 });
    try {
        const response = await fetch('/api/start-transfer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: selectedFile })
        });
        const result = await response.json();
        if (!response.ok || result.error) {
            addEvent(`Transfer rejected: ${result.error || response.status}`);
        } else {
            addEvent(`Transfer queued: ${result.filename} — bridge will pick it up`);
        }
    } catch (e) {
        addEvent(`Transfer request failed: ${e}`);
    } finally {
        startInFlight = false;
        await pollOnce();
        document.getElementById('btn-start').disabled = transferRunning;
    }
});

document.getElementById('btn-stop').addEventListener('click', async () => {
    await fetch('/api/stop-transfer', { method: 'POST' });
    addEvent('Transfer stopped');
});

document.getElementById('btn-link-down').addEventListener('click', async () => {
    const response = await fetch('/api/link-down', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sw1: 's1', sw2: 's2' })
    });
    const result = await response.json();
    addEvent(`Link failure: ${result.sw1} <-> ${result.sw2}`);
    if (result.applied) {
        updateLinkStatus(result.sw1, result.sw2, 'down');
    }
});

document.getElementById('btn-link-up').addEventListener('click', async () => {
    const response = await fetch('/api/link-up', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sw1: 's1', sw2: 's2' })
    });
    const result = await response.json();
    addEvent(`Link restored: ${result.sw1} <-> ${result.sw2}`);
    if (result.applied) {
        updateLinkStatus(result.sw1, result.sw2, 'up');
    }
});

document.getElementById('btn-server-down').addEventListener('click', async () => {
    const host = document.getElementById('server-target').value;
    addEvent(`Killing ${host} — probing twice, 5s apart`);
    const response = await fetch('/api/server-down', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host })
    });
    const result = await response.json();
    addEvent(`Server ${host}: ${result.error || result.status || 'down'}`);
    if (result.applied) updateHostStatus(host, 'down');
});

document.getElementById('btn-server-up').addEventListener('click', async () => {
    const host = document.getElementById('server-target').value;
    addEvent(`Starting ${host}`);
    const response = await fetch('/api/server-up', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host })
    });
    const result = await response.json();
    addEvent(`Server ${host}: ${result.error || result.status || 'up'}`);
    if (result.applied) updateHostStatus(host, 'up');
});

document.getElementById('btn-refresh-files').addEventListener('click', loadFiles);

// Functions
function eventClass(text) {
    // Severity markers: backend lines carry [RECOVERY]/[LINK]/[DONE]/[FAIL];
    // local notices are matched by keyword. Unknown lines stay unstyled.
    if (text.includes('[RECOVERY]')) return 'evt-recovery';
    if (text.includes('[SERVER]')) return 'evt-server';
    if (text.includes('[RETRY]')) return 'evt-retry';
    if (text.includes('[RESUME]')) return 'evt-resume';
    if (text.includes('[FAILOVER]')) return 'evt-failover';
    if (text.includes('[FAIL]') || text.includes('Transfer failed')
        || text.includes('Transfer rejected')) return 'evt-fail';
    if (text.includes('[DONE]') || text.includes('Transfer complete')) return 'evt-done';
    if (text.includes('[LINK]') || text.includes('Link failure')
        || text.includes('Link restored')) return 'evt-link';
    return '';
}

function addEvent(message) {
    const log = document.getElementById('event-log');
    const entry = document.createElement('div');
    entry.className = 'event-entry';
    const cls = eventClass(message);
    if (cls) entry.className += ' ' + cls;
    const timestamp = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="event-timestamp">[${timestamp}]</span> ${message}`;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
}

function updateProgress(state) {
    // Defensive: a missing field must not throw. An exception inside the polling
    // callback would freeze this panel while the rest of the dashboard keeps ticking.
    state = state || {};
    const bar = document.getElementById('progress-bar');
    const text = document.getElementById('progress-text');
    const chunks = document.getElementById('chunks-value');
    const throughput = document.getElementById('throughput-value');

    const progress = Number(state.progress) || 0;
    const sent = Number(state.chunks_sent) || 0;
    const total = Number(state.total_chunks) || 0;
    const tput = Number(state.throughput) || 0;
    const phase = state.phase || (state.running ? 'running' : '');

    updateActiveServer(state.server);

    bar.style.width = `${progress}%`;
    if (phase === 'retrying') {
        text.textContent = `${state.server || 'server'} unreachable, retrying `
            + `${state.attempt || 1}/${state.max_attempts || 2} `
            + `(chunk ${sent}/${total})`;
        bar.className = 'running retrying';
    } else if (phase === 'resuming') {
        text.textContent = `Resuming at chunk ${sent}/${total}`;
        bar.className = 'running resuming';
    } else if (phase === 'failover') {
        text.textContent = `Restarting on backup server ${state.server || 'h4'} — ${sent}/${total}`;
        bar.className = 'running failover';
    } else if (state.running) {
        text.textContent = `Transferring ${state.filename}: ${progress.toFixed(1)}%`;
        bar.className = 'running';
    } else if (state.result && state.result.status === 'done') {
        text.textContent = `Idle — last transfer ${state.filename} completed at `
            + `${state.completed_at || '--:--:--'} (CRC32 verified, ${tput.toFixed(0)} KB/s)`;
        bar.style.width = '0%';
        bar.className = '';
    } else if (state.result && state.result.status === 'failed') {
        text.textContent = `Idle — last transfer ${state.filename} failed at `
            + `${state.completed_at || '--:--:--'} (${state.result.error || 'unknown error'})`;
        bar.style.width = '0%';
        bar.className = '';
    } else if (state.result && state.result.status === 'stopped') {
        text.textContent = `Idle — transfer ${state.filename} stopped by user at `
            + `${state.completed_at || '--:--:--'}`;
        bar.style.width = '0%';
        bar.className = '';
    } else if (state.filename) {
        text.textContent = `Waiting for ${state.filename} to start`;
        bar.className = 'running';
    } else {
        text.textContent = 'No transfer running';
        bar.className = '';
    }
    chunks.textContent = `${sent} / ${total}`;
    throughput.textContent = `${tput.toFixed(1)} KB/s`;
    renderFlow(state);

    // Update chart (skipped when the chart library failed to load)
    throughputData.push(tput);
    if (throughputData.length > 50) throughputData.shift();
    if (throughputChart) {
        throughputChart.data.labels = Array.from({ length: throughputData.length }, (_, i) => i);
        throughputChart.data.datasets[0].data = throughputData;
        throughputChart.update();
    }
}

function updateHostStatus(host, status) {
    const el = document.getElementById('host-' + host);
    if (!el) return;
    const wasDown = el.classList.contains('down') || el.classList.contains('unknown');
    el.classList.remove('down', 'recovering', 'unknown');
    if (status === 'down') {
        el.classList.remove('active-server');
        el.classList.add('down');
    } else if (status === 'unknown') {
        el.classList.add('unknown');
    } else if (status === 'up' && wasDown) {
        el.classList.add('recovering');
        setTimeout(() => el.classList.remove('recovering'), 1500);
    }
}

function updateActiveServer(host) {
    document.querySelectorAll('#topology-svg .host.active-server').forEach(el => {
        el.classList.remove('active-server');
    });
    const el = host ? document.getElementById('host-' + host) : null;
    if (el && !el.classList.contains('down')) el.classList.add('active-server');
}

function updateServerStatus(servers) {
    if (!servers) return;
    const down = [];
    for (const host of ['h3', 'h4']) {
        const s = servers[host];
        if (!s) continue;
        const status = s.alive === true ? 'up' : (s.alive === false ? 'down' : 'unknown');
        if (status === 'down') down.push(host);
        updateHostStatus(host, status);
    }
    const el = document.getElementById('server-status-text');
    if (el) el.textContent = down.length ? `Server ${down.join(', ')} down` : 'Servers up';
}

function renderRetryRing(host) {
    const g = flowLayer();
    if (!g) return;
    let ring = document.getElementById('retry-ring');
    if (!host) {
        if (ring) ring.remove();
        return;
    }
    if (!ring) {
        ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        ring.setAttribute('id', 'retry-ring');
        ring.setAttribute('class', 'retry-ring');
        ring.setAttribute('r', '34');
        g.appendChild(ring);
    }
    const p = FLOW_NODES[host];
    if (p) {
        ring.setAttribute('cx', p.x);
        ring.setAttribute('cy', p.y);
    }
}

function updateLinkStatus(sw1, sw2, status) {
    // Link elements are stored in dashboard.html order (link-s1-s2); events
    // may name the pair either way round, so try both.
    const link = document.getElementById(`link-${sw1}-${sw2}`)
        || document.getElementById(`link-${sw2}-${sw1}`);
    if (link) {
        if (status === 'down') {
            link.classList.add('down');
        } else {
            link.classList.remove('down');
        }
    }
}

// Packet-flow animation on the topology SVG. Visualization only: it mirrors
// the network state shown on this page, it does not observe real packets.
// Model mirrors dashboard.html (viewBox centers, access links, 6-link mesh).
const FLOW_NODES = {
    h1: { x: 90, y: 75 }, h2: { x: 90, y: 275 },
    h3: { x: 560, y: 75 }, h4: { x: 560, y: 275 },
    s1: { x: 240, y: 75 }, s2: { x: 440, y: 75 },
    s3: { x: 240, y: 275 }, s4: { x: 440, y: 275 },
};
const FLOW_ACCESS = { h1: 's1', h2: 's3', h3: 's2', h4: 's4' };
const FLOW_MESH = [['s1', 's2'], ['s1', 's3'], ['s1', 's4'],
                   ['s2', 's3'], ['s2', 's4'], ['s3', 's4']];
// Transfers originate on h1 (the bridge lives there) toward the primary
// server h3. The result payload does not name the receiving server, so h3 is
// a documented assumption, not a measured fact.
const FLOW_SRC = 'h1';
let flowDest = 'h3';
let flowPathKey = '';

function linkElement(a, b) {
    return document.getElementById(`link-${a}-${b}`)
        || document.getElementById(`link-${b}-${a}`);
}

function linkIsDown(a, b) {
    const el = linkElement(a, b);
    return el ? el.classList.contains('down') : false;
}

function computeFlowPath(srcHost, dstHost) {
    // BFS: host -> its switch -> mesh (skipping down links) ->
    // destination switch -> host. Same shape of answer as the controller's
    // Dijkstra, computed from the same link state shown on screen.
    const srcSw = FLOW_ACCESS[srcHost];
    const dstSw = FLOW_ACCESS[dstHost];
    if (!srcSw || !dstSw) return null;
    if (linkIsDown(srcHost, srcSw) || linkIsDown(dstSw, dstHost)) return null;
    const prev = { [srcSw]: null };
    const queue = [srcSw];
    while (queue.length) {
        const cur = queue.shift();
        if (cur === dstSw) break;
        for (const [a, b] of FLOW_MESH) {
            const nxt = (a === cur) ? b : (b === cur) ? a : null;
            if (nxt === null || (nxt in prev) || linkIsDown(cur, nxt)) continue;
            prev[nxt] = cur;
            queue.push(nxt);
        }
    }
    if (!(dstSw in prev)) return null;
    const switches = [];
    for (let n = dstSw; n !== null; n = prev[n]) switches.unshift(n);
    return [srcHost, ...switches, dstHost];
}

function flowLayer() {
    const svg = document.getElementById('topology-svg');
    if (!svg) return null;
    let g = document.getElementById('flow-layer');
    if (!g) {
        g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
        g.setAttribute('id', 'flow-layer');
        g.setAttribute('class', 'flow-layer');
        svg.appendChild(g);
    }
    return g;
}

function flowSegments(nodes) {
    // Build each segment from the link's own endpoints, oriented in the
    // direction of travel. Center-to-center points drift off diagonal links.
    const parts = [];
    for (let i = 0; i < nodes.length - 1; i++) {
        const a = nodes[i];
        const b = nodes[i + 1];
        const el = linkElement(a, b);
        const p = FLOW_NODES[a];
        if (!el || !p) continue;
        let x1 = Number(el.getAttribute('x1'));
        let y1 = Number(el.getAttribute('y1'));
        let x2 = Number(el.getAttribute('x2'));
        let y2 = Number(el.getAttribute('y2'));
        if (Math.hypot(x2 - p.x, y2 - p.y) < Math.hypot(x1 - p.x, y1 - p.y)) {
            [x1, x2] = [x2, x1];
            [y1, y2] = [y2, y1];
        }
        parts.push(`M ${x1} ${y1} L ${x2} ${y2}`);
    }
    return parts.join(' ');
}

function clearFlow() {
    const g = document.getElementById('flow-layer');
    if (g) {
        g.innerHTML = '';
        g.classList.remove('retrying');
    }
    flowPathKey = '';
    document.querySelectorAll('#topology-svg .link.active').forEach(el => {
        el.classList.remove('active');
    });
}

function highlightFlowPath(nodes) {
    document.querySelectorAll('#topology-svg .link.active').forEach(el => {
        el.classList.remove('active');
    });
    for (let i = 0; i < nodes.length - 1; i++) {
        const el = linkElement(nodes[i], nodes[i + 1]);
        if (el) el.classList.add('active');
    }
}

function renderFlow(state) {
    // Dots ride the progress fraction, so the motion is honest: stalled
    // progress means still dots. A mid-transfer reroute recomputes the path
    // and the dots continue at the same fraction on the new route — the jump
    // *is* the reroute made visible.
    const total = Number(state.total_chunks) || 0;
    const phase = state.phase || '';
    if (phase !== 'retrying') {
        if (state.server === 'h4') flowDest = 'h4';
        else if (state.server === 'h3') flowDest = 'h3';
    }
    if (!state.running || total <= 0) {
        clearFlow();
        renderRetryRing(null);
        return;
    }
    const retrying = phase === 'retrying';
    const nodes = computeFlowPath(FLOW_SRC, flowDest);
    if (!nodes) {
        clearFlow();
        renderRetryRing(null);
        return;
    }
    const key = nodes.join('>');
    const g = flowLayer();
    if (!g) return;
    g.classList.toggle('retrying', retrying);
    if (key !== flowPathKey) {
        g.innerHTML = '';
        flowPathKey = key;
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('id', 'flow-line');
        path.setAttribute('class', 'flow-line');
        path.setAttribute('d', flowSegments(nodes));
        g.appendChild(path);
        highlightFlowPath(nodes);
    }
    renderRetryRing(retrying ? flowDest : null);
}

async function loadFiles() {
    const response = await fetch('/api/files');
    const files = await response.json();
    const list = document.getElementById('files-list');
    list.innerHTML = '';

    for (const [server, fileList] of Object.entries(files)) {
        for (const file of fileList) {
            const item = document.createElement('div');
            item.className = 'file-item';
            item.innerHTML = `
                <span><span class="file-server">${server}</span><span class="file-name">${file.name}</span></span>
                <span class="file-size">${formatSize(file.size)}</span>
            `;
            list.appendChild(item);
        }
    }

    if (!files.h3?.length && !files.h4?.length) {
        list.innerHTML = '<p>No files stored yet</p>';
    }
}

async function loadBrowse() {
    const response = await fetch('/api/browse');
    const data = await response.json();
    const list = document.getElementById('browse-list');
    list.innerHTML = '';

    if (!data.files.length) {
        list.innerHTML = '<p>Drop files into <code>shared_files/</code> and click Refresh.</p>';
        return;
    }

    for (const file of data.files) {
        const item = document.createElement('div');
        item.className = 'file-item';
        item.innerHTML = `
            <span class="file-name">${file.name}</span>
            <span class="file-size">${formatSize(file.size)}</span>
        `;
        item.addEventListener('click', () => {
            document.querySelectorAll('#browse-list .file-item').forEach(el => el.classList.remove('selected'));
            item.classList.add('selected');
            selectedFile = file.name;
            addEvent(`Selected: ${file.name}`);
        });
        list.appendChild(item);
    }
}

function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Initial load
loadFiles();
loadBrowse();
document.getElementById('btn-refresh-browse').addEventListener('click', loadBrowse);
async function pollOnce() {
    const response = await fetch('/api/metrics');
    const metrics = await response.json();
    const rt = metrics.recovery_time;
    const kind = metrics.recovery_kind;
    const target = metrics.recovery_target;
    document.getElementById('recovery-value').textContent =
        rt != null
            ? `${rt.toFixed(2)}s${kind ? ` (${kind}${target ? ' ' + target : ''})` : ''}`
            : 'N/A';
    try {
        const res = await fetch('/api/transfer-result');
        const tr = await res.json();
        const trKey = (tr.request_id || tr.filename) + tr.status;
        if ((tr.status === 'done' || tr.status === 'failed' || tr.status === 'stopped') && lastResultSeen !== trKey) {
            lastResultSeen = trKey;
            if (tr.status === 'done') {
                addEvent(`Transfer complete: ${tr.filename} (CRC32 verified)`);
                loadFiles();
            } else if (tr.status === 'stopped') {
                addEvent(`Transfer stopped: ${tr.filename}`);
            } else {
                addEvent(`Transfer failed: ${tr.filename} — ${tr.error || 'unknown error'}`);
            }
        }
    } catch (e) { /* bridge result not available yet */ }
    // Always sync progress + events: the 'transfer_state' socket emit only
    // fires on start/stop, so without this the bar and metrics freeze mid-run.
    try {
        const st = await fetch('/api/status');
        const sdata = await st.json();
        if (sdata.transfer) {
            updateProgress(sdata.transfer);
            transferRunning = !!sdata.transfer.running;
            if (!startInFlight) {
                document.getElementById('btn-start').disabled = transferRunning;
            }
        }
        updateServerStatus(sdata.servers);
        const b = sdata.bridge;
        if (b) {
            const bridgeEl = document.getElementById('bridge-value');
            if (bridgeEl) {
                bridgeEl.textContent = b.alive
                    ? `alive (pid ${b.pid}, ${b.age_s}s ago)`
                    : 'not seen — is mininet/topology.py running?';
            }
        }
        const eventLog = document.getElementById('event-log');
        eventLog.innerHTML = '';
        for (const entry of sdata.events) {
            const div = document.createElement('div');
            div.className = 'event-entry';
            const cls = eventClass(entry);
            if (cls) div.className += ' ' + cls;
            div.textContent = entry;
            eventLog.appendChild(div);
        }
    } catch (e) { /* status not available yet */ }
}
setInterval(pollOnce, 2000);
pollOnce();
