/**
 * Dashboard frontend logic — WebSocket events, topology rendering,
 * transfer control, metrics charts, file listing.
 */

const socket = io();
const ctx = document.getElementById('throughput-chart').getContext('2d');

// Throughput chart
const throughputChart = new Chart(ctx, {
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

// Socket events
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

// Button handlers
document.getElementById('btn-start').addEventListener('click', async () => {
    if (!selectedFile) {
        addEvent('Pick a file from Shared Files first');
        return;
    }
    const response = await fetch('/api/start-transfer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: selectedFile })
    });
    const result = await response.json();
    if (result.error) {
        addEvent(`Transfer failed: ${result.error}`);
    } else {
        addEvent(`Transfer queued: ${result.filename} — bridge will pick it up`);
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
});

document.getElementById('btn-link-up').addEventListener('click', async () => {
    const response = await fetch('/api/link-up', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sw1: 's1', sw2: 's2' })
    });
    const result = await response.json();
    addEvent(`Link restored: ${result.sw1} <-> ${result.sw2}`);
});

document.getElementById('btn-refresh-files').addEventListener('click', loadFiles);

// Functions
function addEvent(message) {
    const log = document.getElementById('event-log');
    const entry = document.createElement('div');
    entry.className = 'event-entry';
    const timestamp = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="event-timestamp">[${timestamp}]</span> ${message}`;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
}

function updateProgress(state) {
    const bar = document.getElementById('progress-bar');
    const text = document.getElementById('progress-text');
    const chunks = document.getElementById('chunks-value');
    const throughput = document.getElementById('throughput-value');

    bar.style.width = `${state.progress}%`;
    text.textContent = state.running
        ? `Transferring ${state.filename}: ${state.progress.toFixed(1)}%`
        : 'No transfer running';
    chunks.textContent = `${state.chunks_sent} / ${state.total_chunks}`;
    throughput.textContent = `${state.throughput.toFixed(1)} KB/s`;

    // Update chart
    throughputData.push(state.throughput);
    if (throughputData.length > 50) throughputData.shift();
    throughputChart.data.labels = Array.from({ length: throughputData.length }, (_, i) => i);
    throughputChart.data.datasets[0].data = throughputData;
    throughputChart.update();
}

function updateLinkStatus(sw1, sw2, status) {
    const linkId = `link-${sw1}-${sw2}`;
    const link = document.getElementById(linkId);
    if (link) {
        if (status === 'down') {
            link.classList.add('down');
        } else {
            link.classList.remove('down');
        }
    }
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
setInterval(async () => {
    const response = await fetch('/api/metrics');
    const metrics = await response.json();
    document.getElementById('recovery-value').textContent =
        metrics.recovery_time ? `${metrics.recovery_time.toFixed(2)}s` : 'N/A';
    try {
        const res = await fetch('/api/transfer-result');
        const tr = await res.json();
        if ((tr.status === 'done' || tr.status === 'failed') && lastResultSeen !== tr.filename + tr.status) {
            lastResultSeen = tr.filename + tr.status;
            if (tr.status === 'done') {
                addEvent(`Transfer complete: ${tr.filename} (CRC32 verified)`);
            } else {
                addEvent(`Transfer failed: ${tr.filename} — ${tr.error || 'unknown error'}`);
            }
            loadFiles();
        }
    } catch (e) { /* bridge result not available yet */ }
}, 2000);
