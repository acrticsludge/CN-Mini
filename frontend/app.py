"""
Flask web dashboard for the Distributed Backup System.
Serves a single-page app with live topology, transfer control, metrics, and event log.
"""
import json
import os
import sys
import threading
import time
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-not-for-prod')
socketio = SocketIO(
    app,
    cors_allowed_origins='*',
    async_mode='threading'
)

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, BASE_DIR)
from common.protocol import CHUNK_SIZE

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
STORED_DIR = os.path.join(BASE_DIR, 'stored_files')
SHARED_DIR = os.path.join(BASE_DIR, 'shared_files')
REQUEST_FILE = os.path.join(SHARED_DIR, 'transfer_request.json')
RESULT_FILE = os.path.join(SHARED_DIR, 'transfer_result.json')
PROGRESS_FILE = os.path.join(SHARED_DIR, 'transfer_progress.json')
EVENTS_FILE = os.path.join(SHARED_DIR, 'transfer_events.json')
CANCEL_FILE = os.path.join(SHARED_DIR, 'transfer_cancel.json')
LINK_REQUEST_FILE = os.path.join(SHARED_DIR, 'link_request.json')
LINK_RESULT_FILE = os.path.join(SHARED_DIR, 'link_result.json')
SERVER_REQUEST_FILE = os.path.join(SHARED_DIR, 'server_request.json')
SERVER_RESULT_FILE = os.path.join(SHARED_DIR, 'server_result.json')
SERVER_STATUS_FILE = os.path.join(SHARED_DIR, 'server_status.json')
SERVER_STORAGE_FILE = os.path.join(SHARED_DIR, 'server_storage.json')

# Global state
transfer_state = {
    'running': False,
    'filename': None,
    'request_id': None,
    'progress': 0,
    'chunks_sent': 0,
    'total_chunks': 0,
    'throughput': 0.0,
    'start_time': None,
    'recovery_time': None,
    'recovery_kind': None,
    'recovery_target': None,
    'duration': None,
    'link_down_at': None,
    'server_down_at': None,
    'phase': None,
    'attempt': 0,
    'max_attempts': 2,
    'resume_from': 0,
    'server': None,
    'retry_wait_s': None,
    'last_chunk_at': None,
    'chunk_idle_s': None,
    'link_stall_warned': False,
    'result': None,
    'completed_at': None,
}

# Serialises concurrent POSTs: two clicks arriving together must not both pass
# the busy check and both write. The second one waits, re-checks, and gets 409.
_start_lock = threading.Lock()
_request_counter = 0

# A queued run with no terminal result counts as busy for this long, covering
# the pickup window where the bridge has consumed the request but not yet
# written progress. Older than this with no result, the run is presumed dead.
PENDING_GRACE = 120

# A chunk advance within this window may be an in-flight ACK from before the
# blackhole; counting that as "recovered" once logged a bogus 0.3 s and
# spent the one-shot clock while the transfer then stalled forever. Advances
# inside this window are stragglers: the clock keeps running.
RECOVERY_STRAGGLER_GRACE = 3.0

# A confirmed link outage with no later chunk movement is reported as a stall
# after this longer window, so a flat progress bar is not mistaken for normal
# rerouted traffic.
LINK_STALL_GRACE = 5.0

TERMINAL_STATUSES = ('done', 'failed', 'stopped')


def _new_request_id():
    """Unique id per click so results can only ever match their own run."""
    global _request_counter
    _request_counter += 1
    return f'{int(time.time() * 1000)}-{os.getpid()}-{_request_counter}'


def _matches_current(doc):
    """True when a progress/result file belongs to the currently queued run.

    Matches on request_id; falls back to filename so files written mid-flight
    by an older bridge (no id) are still picked up instead of orphaned.
    """
    if not doc or transfer_state.get('filename') is None:
        return False
    current_id = transfer_state.get('request_id')
    if current_id and doc.get('request_id'):
        return doc.get('request_id') == current_id
    return doc.get('filename') == transfer_state['filename']

EVENT_LOG_LIMIT = 200
event_log = []
_events_ingested = 0


def _read_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_json_atomic(path, data):
    """Write JSON atomically so the bridge never reads a half-written file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


# A progress file older than this is a corpse from a dead bridge, not a live
# transfer — treat it as idle so the dashboard never wedges "busy" forever.
STALE_PROGRESS_AGE = 600


def _bridge_busy():
    """True when the bridge still has work: a pending request or live progress."""
    if os.path.exists(REQUEST_FILE):
        return True
    progress = _read_json(PROGRESS_FILE)
    if progress and progress.get('running'):
        try:
            age = time.time() - os.path.getmtime(PROGRESS_FILE)
        except OSError:
            age = 0
        if age < STALE_PROGRESS_AGE:
            return True
    # Pickup window: we queued a run that the bridge has consumed but not yet
    # reflected in any file. Until its result lands (or it goes stale), a new
    # click must wait — otherwise it clobbers the run it cannot yet see.
    if transfer_state.get('running') and transfer_state.get('request_id'):
        res = _read_json(RESULT_FILE)
        if not (res and _matches_current(res)
                and res.get('status') in TERMINAL_STATUSES):
            started = transfer_state.get('start_time') or 0
            if time.time() - started < PENDING_GRACE:
                return True
    return False


def _bridge_status():
    """Liveness for the UI, from the bridge's heartbeat file."""
    alive_path = os.path.join(SHARED_DIR, 'bridge_alive.json')
    info = _read_json(alive_path)
    if not info:
        return {'alive': False, 'age_s': None, 'pid': None}
    try:
        age = time.time() - os.path.getmtime(alive_path)
    except OSError:
        age = None
    return {'alive': age is not None and age < 10,
            'age_s': round(age, 1) if age is not None else None,
            'pid': info.get('pid')}


def _server_status():
    """Liveness for h3/h4. A stale file means unknown, not down."""
    info = _read_json(SERVER_STATUS_FILE)
    try:
        age = time.time() - os.path.getmtime(SERVER_STATUS_FILE)
    except OSError:
        age = None
    out = {}
    for host in ('h3', 'h4'):
        entry = (info or {}).get(host) or {}
        alive = entry.get('alive')
        if age is None or age > 10:
            alive = None
        out[host] = {'alive': alive, 'pid': entry.get('pid'),
                     'since': entry.get('since'),
                     'age_s': round(age, 1) if age is not None else None}
    return out


# Per-server storage quota, persisted in shared_files/server_storage.json.
# quota_mb == 0 means unlimited. used_mb is recomputed from disk on every read.
DEFAULT_QUOTA_MB = 1000


def _load_storage_quotas():
    """Return {host: quota_mb} for h3/h4, falling back to the default."""
    data = _read_json(SERVER_STORAGE_FILE) or {}
    quotas = {}
    for host in ('h3', 'h4'):
        try:
            quotas[host] = int(data.get(host, DEFAULT_QUOTA_MB))
        except (TypeError, ValueError):
            quotas[host] = DEFAULT_QUOTA_MB
    return quotas


def _save_storage_quotas(quotas):
    _write_json_atomic(SERVER_STORAGE_FILE, quotas)


def _used_bytes(host):
    """Sum of non-.part bytes stored for a host (raw bytes, for capacity checks)."""
    path = os.path.join(STORED_DIR, host)
    total = 0
    if os.path.isdir(path):
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if os.path.isfile(full) and not name.endswith('.part'):
                try:
                    total += os.path.getsize(full)
                except OSError:
                    pass
    return total


def _used_mb(host):
    """Sum of non-.part bytes stored for a host, in whole MB (display only)."""
    return _used_bytes(host) // (1024 * 1024)


def _storage_snapshot():
    """{host: {quota_mb, used_mb, free_mb}} recomputed from disk each read."""
    quotas = _load_storage_quotas()
    out = {}
    for host in ('h3', 'h4'):
        used = _used_mb(host)
        quota = quotas[host]
        free = None if quota == 0 else max(quota - used, 0)
        out[host] = {'quota_mb': quota, 'used_mb': used, 'free_mb': free}
    return out


def _sync_transfer_state():
    """Fold the bridge's progress/result/events into dashboard state.

    The bridge is a separate process writing plain files, so these are the only
    channel the dashboard has to a running transfer. Keep this tolerant: a
    partially written or missing file must not take the endpoint down.
    """
    progress = _read_json(PROGRESS_FILE)
    if _matches_current(progress):
        prev_chunks = transfer_state.get('chunks_sent', 0)
        now = time.time()
        current_chunk = progress.get('chunk', 0)
        transfer_state['chunk_idle_s'] = None
        transfer_state['running'] = bool(progress.get('running'))
        transfer_state['chunks_sent'] = progress.get('chunk', 0)
        transfer_state['total_chunks'] = progress.get('total', 0)
        transfer_state['throughput'] = progress.get('throughput', 0.0)
        transfer_state['phase'] = progress.get('phase')
        transfer_state['attempt'] = progress.get('attempt', 0)
        transfer_state['max_attempts'] = progress.get('max_attempts', 2)
        transfer_state['resume_from'] = progress.get('resume_from', 0)
        transfer_state['server'] = progress.get('server')
        transfer_state['retry_wait_s'] = progress.get('retry_wait_s')
        if current_chunk > prev_chunks:
            transfer_state['last_chunk_at'] = now
        last_chunk_at = transfer_state.get('last_chunk_at')
        if current_chunk > 0 and last_chunk_at:
            transfer_state['chunk_idle_s'] = round(now - last_chunk_at, 1)
        # The bar/text read state.progress, which used to be 0 for the whole
        # run and 100 at the end — so a 1 GB transfer sat at 0.0% for minutes
        # while chunks visibly flowed underneath it.
        total = progress.get('total', 0) or 0
        if total:
            transfer_state['progress'] = round(progress.get('chunk', 0) / total * 100, 1)
        # Recovery clock: chunks moving again after a confirmed link-down
        # means traffic is flowing on the new path. The gap between the two
        # is the reroute time the demo needs — measured where traffic is
        # observable, since Flask cannot see inside the controller.
        if (current_chunk > prev_chunks
                and transfer_state.get('link_down_at') is not None
                and transfer_state.get('recovery_time') is None):
            dt = now - transfer_state['link_down_at']
            if dt < RECOVERY_STRAGGLER_GRACE:
                pass  # straggler ACK from before the blackhole; keep waiting
            else:
                transfer_state['recovery_time'] = round(dt, 1)
                transfer_state['recovery_kind'] = 'link'
                transfer_state['link_down_at'] = None
                log_event(f"[RECOVERY] Traffic flowing again "
                          f"{dt:.1f}s after link down "
                          f"({transfer_state['filename']}: "
                          f"chunk {current_chunk}/{total})")
        # A stalled transfer needs its own explicit warning. The topology can
        # already display an alternate route while no chunks are moving, so say
        # that the route is unconfirmed traffic.
        if (transfer_state.get('running')
                and current_chunk > 0
                and current_chunk <= prev_chunks
                and transfer_state.get('link_down_at') is not None
                and transfer_state.get('recovery_time') is None
                and not transfer_state.get('link_stall_warned')):
            dt = now - transfer_state['link_down_at']
            if dt >= LINK_STALL_GRACE:
                transfer_state['link_stall_warned'] = True
                log_event(f"[LINK] {transfer_state.get('recovery_target')}: no "
                          f"chunk advance for {dt:.0f}s; transfer stalled at "
                          f"chunk {current_chunk}/{total}")
        # Server recovery uses the same clock and the same one-shot rule.
        if (progress.get('phase') == 'retrying'
                and transfer_state.get('server_down_at') is None):
            transfer_state['server_down_at'] = now
            transfer_state['recovery_target'] = (
                transfer_state.get('recovery_target') or progress.get('server'))
        if (current_chunk > prev_chunks
                and transfer_state.get('server_down_at') is not None
                and transfer_state.get('recovery_time') is None):
            dt = now - transfer_state['server_down_at']
            if dt >= RECOVERY_STRAGGLER_GRACE:
                target = (transfer_state.get('recovery_target')
                          or progress.get('server') or transfer_state.get('server'))
                current = (progress.get('server') or transfer_state.get('server')
                           or target)
                transfer_state['recovery_time'] = round(dt, 1)
                transfer_state['recovery_kind'] = 'server'
                transfer_state['server_down_at'] = None
                if current != target:
                    transfer_state['recovery_target'] = f'{target}->{current}'
                    log_event(f"[RECOVERY] {target} did not return; {current} "
                              f"took over after {dt:.1f}s "
                              f"(chunk {current_chunk}/{total})")
                else:
                    transfer_state['recovery_target'] = target
                    log_event(f"[RECOVERY] Server {target} back after {dt:.1f}s; "
                              f"resumed at chunk {current_chunk}/{total}")
        if not progress.get('running') and not progress.get('cancelled'):
            transfer_state['progress'] = 100

    result = _read_json(RESULT_FILE)
    if _matches_current(result):
        # Stamp the first time we see this result so the UI can show how
        # long the completed state has been held, rather than looking stuck.
        if transfer_state.get('result') != result:
            transfer_state['completed_at'] = time.strftime('%H:%M:%S')
            started = transfer_state.get('start_time')
            if started:
                transfer_state['duration'] = round(time.time() - started, 1)
            if result.get('status') == 'done':
                log_event(
                    f"[DONE] Transfer done: {result.get('filename')} — "
                    f"{transfer_state.get('chunks_sent', 0)}/"
                    f"{transfer_state.get('total_chunks', 0)} chunks in "
                    f"{transfer_state.get('duration', '?')}s at "
                    f"{transfer_state.get('throughput', 0)} KB/s")
            elif result.get('status') == 'failed':
                log_event(
                    f"[FAIL] Transfer failed: {result.get('filename')} — "
                    f"{result.get('error', 'unknown error')} "
                    f"(after {transfer_state.get('duration', '?')}s)")
        transfer_state['result'] = result
        transfer_state['running'] = False
        if result.get('status') == 'done':
            transfer_state['progress'] = 100

    # The bridge rewrites its event file per transfer (append-only list, capped).
    # Ingest by index rather than deduping by exact string: two identical lines
    # are two real events (a retry, a second connect attempt), and the old
    # string-set dedupe silently dropped them, which is why logs disappeared.
    global _events_ingested
    events = _read_json(EVENTS_FILE)
    if events is not None:
        if len(events) < _events_ingested:
            _events_ingested = 0  # file was reset by a new transfer
        for entry in events[_events_ingested:]:
            event_log.append(entry)
            print(entry, flush=True)
        _events_ingested = len(events)
        # Keep a generous tail; the dashboard renders the most recent ones.
        del event_log[:-EVENT_LOG_LIMIT]


def log_event(message):
    """Add an event to the log and broadcast via WebSocket."""
    timestamp = time.strftime('%H:%M:%S')
    entry = f'[{timestamp}] {message}'
    event_log.append(entry)
    print(entry, flush=True)
    socketio.emit('event', {'message': message, 'timestamp': timestamp})


@app.route('/')
def index():
    return render_template('dashboard.html')


@app.route('/api/files')
def list_files():
    """List all stored files on backup servers."""
    files = {}
    for server in ['h3', 'h4']:
        path = os.path.join(STORED_DIR, server)
        if os.path.exists(path):
            files[server] = [
                {'name': f, 'size': os.path.getsize(os.path.join(path, f))}
                for f in os.listdir(path)
                if os.path.isfile(os.path.join(path, f))
                and not f.endswith('.part')
            ]
        else:
            files[server] = []
    return jsonify(files)


@app.route('/api/partial-files')
def list_partial_files():
    """List interrupted (.part) transfers on backup servers that can be resumed.

    A .part file means a transfer was interrupted before completion. The resume
    point is floor(size / CHUNK_SIZE): every complete chunk in the part file is
    already stored, so the client resumes from that chunk index.
    """
    CHUNK_SIZE_LOCAL = CHUNK_SIZE
    partials = {}
    for server in ['h3', 'h4']:
        path = os.path.join(STORED_DIR, server)
        partials[server] = []
        if os.path.exists(path):
            for f in sorted(os.listdir(path)):
                full = os.path.join(path, f)
                if not f.endswith('.part') or not os.path.isfile(full):
                    continue
                size = os.path.getsize(full)
                chunks = size // CHUNK_SIZE_LOCAL
                partials[server].append({
                    'name': f[:-5],  # strip .part suffix -> original filename
                    'size': size,
                    'chunks': chunks,
                    'partial_name': f,
                })
    return jsonify(partials)


@app.route('/api/browse')
def browse_files():
    """List files in shared_files/ for the dashboard file picker."""
    os.makedirs(SHARED_DIR, exist_ok=True)
    # /tmp/shared inside Mininet is bind-mounted to this directory, so the
    # bridge's and link-watcher's bookkeeping files show up here too. They are
    # not transferable files — hide them from the picker.
    internal = {'bridge.lock', 'bridge_alive.json',
                'link_request.json', 'link_result.json'}
    files = [
        {'name': f, 'size': os.path.getsize(os.path.join(SHARED_DIR, f))}
        for f in sorted(os.listdir(SHARED_DIR))
        if os.path.isfile(os.path.join(SHARED_DIR, f))
        and not f.startswith('transfer_')
        and f not in internal
    ]
    return jsonify({'directory': 'shared_files', 'files': files})


@app.route('/api/transfer-result')
def transfer_result():
    """Read the latest bridge result, if any."""
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE) as f:
            return jsonify(json.load(f))
    return jsonify({'status': 'none'})

@app.route('/api/start-transfer', methods=['POST'])
def start_transfer():
    """Queue a file transfer: writes a request the Mininet bridge picks up."""
    data = request.get_json(force=True, silent=True) or {}
    filename = data.get('filename') or os.path.basename(data.get('file_path', ''))
    resume_from = int(data.get('resume_from', 0) or 0)
    target = data.get('host', 'h3')
    if target not in ('h3', 'h4'):
        target = 'h3'
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': 'Invalid filename'}), 400

    os.makedirs(SHARED_DIR, exist_ok=True)
    src = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(src):
        return jsonify({'error': f'File not found in shared_files/: {filename}'}), 404
    file_size = os.path.getsize(src)
    MB = 1024 * 1024

    def _free_bytes(host, quota_mb):
        # Bytes still available on a host. None means unlimited (quota 0).
        # size == free space is allowed; only strictly-insufficient is full.
        if quota_mb == 0:
            return None
        return max(quota_mb * MB - _used_bytes(host), 0)

    def _is_full(free_bytes):
        return free_bytes is not None and file_size > free_bytes

    storage = _storage_snapshot()
    target_free = _free_bytes(target, storage[target]['quota_mb'])
    other = 'h4' if target == 'h3' else 'h3'
    other_free = _free_bytes(other, storage[other]['quota_mb'])
    routed_to = target
    if _is_full(target_free):
        if _is_full(other_free):
            log_event(f'[STORAGE] Both servers full — cannot store {filename} '
                      f'(need {file_size} bytes)')
            return jsonify({'error': f'Both servers full — cannot store {filename}'}), 409
        log_event(f'[STORAGE] {target} full for {filename} (need {file_size} bytes, '
                  f'have {target_free} bytes); routing to {other}')
        routed_to = other

    with _start_lock:
        _sync_transfer_state()
        # The bridge is single-threaded: a second request while one is pending or
        # running does not queue — it overwrites REQUEST_FILE and deletes the live
        # run's progress/result mid-flight. Reject it instead so a rapid double
        # click can never silently eat the first transfer.
        if _bridge_busy():
            return jsonify({'error': 'Transfer already running — wait for it or Stop it',
                            'filename': transfer_state.get('filename')}), 409

        # Clear stale bridge bookkeeping so this run's progress is unambiguous.
        for p in (RESULT_FILE, PROGRESS_FILE, EVENTS_FILE, CANCEL_FILE):
            if os.path.exists(p):
                os.unlink(p)
        request_id = _new_request_id()
        _write_json_atomic(REQUEST_FILE, {'filename': filename,
                                          'request_id': request_id,
                                          'resume_from': resume_from,
                                          'host': routed_to})

    # New run, new event file on the bridge side: reset the ingest cursor so
    # the fresh events are picked up from index 0. The dashboard's own history
    # is kept — wiping it on every click hid what just happened.
    global _events_ingested
    _events_ingested = 0

    transfer_state['running'] = True
    transfer_state['filename'] = filename
    transfer_state['request_id'] = request_id
    transfer_state['progress'] = 0
    transfer_state['chunks_sent'] = 0
    transfer_state['total_chunks'] = 0
    transfer_state['throughput'] = 0.0
    transfer_state['start_time'] = time.time()
    transfer_state['result'] = None
    transfer_state['duration'] = None
    transfer_state['recovery_time'] = None
    transfer_state['recovery_kind'] = None
    transfer_state['recovery_target'] = None
    transfer_state['link_down_at'] = None
    transfer_state['server_down_at'] = None
    transfer_state['phase'] = 'queued'
    transfer_state['attempt'] = 0
    transfer_state['max_attempts'] = 2
    transfer_state['resume_from'] = 0
    transfer_state['server'] = None
    transfer_state['storage_from'] = None
    transfer_state['storage_to'] = None
    transfer_state['retry_wait_s'] = None
    transfer_state['last_chunk_at'] = None
    transfer_state['chunk_idle_s'] = None
    transfer_state['link_stall_warned'] = False
    transfer_state['storage_from'] = target if routed_to != target else None
    transfer_state['storage_to'] = routed_to
    # The previous transfer's result is intentionally kept out of the new run's
    # state, but its metrics stay visible in the panel until this one finishes.
    transfer_state['completed_at'] = None

    log_event(f'Transfer queued: {filename} (bridge will pick it up)')
    socketio.emit('transfer_state', transfer_state)
    return jsonify({'status': 'queued', 'filename': filename,
                    'request_id': request_id})


@app.route('/api/stop-transfer', methods=['POST'])
def stop_transfer():
    """Stop the current transfer by telling the bridge to kill its client."""
    transfer_state['running'] = False
    try:
        os.makedirs(SHARED_DIR, exist_ok=True)
        with open(CANCEL_FILE, 'w') as f:
            json.dump({'requested_at': time.time()}, f)
    except Exception:
        pass
    log_event('Transfer stopped by user')
    socketio.emit('transfer_state', transfer_state)
    return jsonify({'status': 'stopped'})


@app.route('/api/link-down', methods=['POST'])
def link_down():
    """Ask Mininet to take a real link down (the Ryu controller then reroutes)."""
    data = request.get_json(force=True, silent=True) or {}
    sw1 = data.get('sw1', 's1')
    sw2 = data.get('sw2', 's2')
    return _request_link(sw1, sw2, 'down')


@app.route('/api/link-up', methods=['POST'])
def link_up():
    """Ask Mininet to restore a real link."""
    data = request.get_json(force=True, silent=True) or {}
    sw1 = data.get('sw1', 's1')
    sw2 = data.get('sw2', 's2')
    return _request_link(sw1, sw2, 'up')


def _request_link(sw1, sw2, state):
    """Write a link request for topology.py's watcher, and report what it did."""
    for name in (sw1, sw2):
        if not name.startswith('s') or not name[1:].isdigit():
            return jsonify({'error': f'Invalid switch name: {name}'}), 400
    if sw1 == sw2:
        return jsonify({'error': 'Pick two different switches'}), 400

    try:
        os.makedirs(SHARED_DIR, exist_ok=True)
        request_id = f'{int(time.time() * 1000)}-{os.getpid()}'
        req = os.path.join(SHARED_DIR, 'link_request.json')
        with open(req, 'w') as f:
            json.dump({'sw1': sw1, 'sw2': sw2, 'state': state,
                       'request_id': request_id,
                       'requested_at': time.time()}, f)
    except Exception as e:
        return jsonify({'error': f'Could not write link request: {e}'}), 500

    # Wait briefly for topology.py to confirm it actually applied the change.
    # The dashboard must reflect the real network, not our intent. A result only
    # counts if it echoes this request's id — a leftover file from an earlier
    # click would otherwise be mistaken for a fresh confirmation.
    result_file = os.path.join(SHARED_DIR, 'link_result.json')
    confirmed = None
    for _ in range(20):
        res = _read_json(result_file)
        if res and res.get('request_id') == request_id \
                and res.get('sw1') == sw1 and res.get('sw2') == sw2 \
                and res.get('state') == state:
            confirmed = res
            break
        time.sleep(0.25)

    verb = 'Link failure' if state == 'down' else 'Link restored'
    if confirmed and confirmed.get('applied'):
        if state == 'down':
            # Start the recovery clock: when chunks advance again, _sync stamps
            # recovery_time and logs the [RECOVERY] line. Fresh episode — any
            # previous measurement belongs to an older failure.
            transfer_state['link_down_at'] = time.time()
            transfer_state['recovery_time'] = None
            transfer_state['recovery_kind'] = 'link'
            transfer_state['recovery_target'] = f'{sw1}-{sw2}'
            transfer_state['link_stall_warned'] = False
        log_event(f'[LINK] {verb}: {sw1} <-> {sw2} confirmed by Mininet')
        socketio.emit('link_event', {'sw1': sw1, 'sw2': sw2, 'status': state})
        return jsonify({'status': state, 'sw1': sw1, 'sw2': sw2, 'applied': True})
    if confirmed:
        log_event(f'[LINK] {verb}: {sw1} <-> {sw2} REJECTED — {confirmed.get("error")}')
        return jsonify({'status': 'error', 'sw1': sw1, 'sw2': sw2,
                        'applied': False, 'error': confirmed.get('error')}), 502

    log_event(f'[LINK] {verb}: {sw1} <-> {sw2} requested but Mininet did not confirm '
              f'(is mininet/topology.py running?)')
    return jsonify({'status': 'unconfirmed', 'sw1': sw1, 'sw2': sw2,
                    'applied': False,
                    'error': 'Mininet did not confirm within 5s — is '
                             'mininet/topology.py running?'}), 504


def _request_server(host, action):
    """Ask Mininet to kill or restart one backup server, and wait for confirmation."""
    if host not in ('h3', 'h4'):
        return jsonify({'error': f'Invalid server: {host}'}), 400
    if action not in ('down', 'up'):
        return jsonify({'error': f'Invalid action: {action}'}), 400
    try:
        os.makedirs(SHARED_DIR, exist_ok=True)
        request_id = f'{int(time.time() * 1000)}-{os.getpid()}'
        _write_json_atomic(SERVER_REQUEST_FILE, {
            'host': host, 'action': action,
            'request_id': request_id,
            'requested_at': time.time(),
        })
    except Exception as e:
        return jsonify({'error': f'Could not write server request: {e}'}), 500

    confirmed = None
    for _ in range(40):
        res = _read_json(SERVER_RESULT_FILE)
        if (res and res.get('request_id') == request_id
                and res.get('host') == host and res.get('action') == action):
            confirmed = res
            break
        time.sleep(0.25)

    if confirmed and confirmed.get('applied'):
        state = confirmed.get('state', action)
        if state == 'down' and transfer_state.get('running'):
            transfer_state['server_down_at'] = time.time()
            transfer_state['recovery_time'] = None
            transfer_state['recovery_target'] = host
        log_event(f"[SERVER] {host} {state} confirmed")
        socketio.emit('server_event', {'host': host, 'status': state})
        return jsonify({'status': state, 'host': host, 'applied': True})
    if confirmed:
        log_event(f"[SERVER] {host} {action} REJECTED — {confirmed.get('error')}")
        return jsonify({'status': 'error', 'host': host,
                        'applied': False, 'error': confirmed.get('error')}), 502

    log_event(f"[SERVER] {host} {action} requested but Mininet did not confirm "
              f"(is mininet/topology.py running?)")
    return jsonify({'status': 'unconfirmed', 'host': host, 'applied': False,
                    'error': 'Mininet did not confirm within 10s — is '
                             'mininet/topology.py running?'}), 504


@app.route('/api/server-down', methods=['POST'])
def server_down():
    data = request.get_json(force=True, silent=True) or {}
    return _request_server(data.get('host', 'h3'), 'down')


@app.route('/api/server-up', methods=['POST'])
def server_up():
    data = request.get_json(force=True, silent=True) or {}
    return _request_server(data.get('host', 'h3'), 'up')


@app.route('/api/bridge')
def bridge_status():
    """Bridge liveness for the dashboard indicator."""
    return jsonify(_bridge_status())


@app.route('/api/status')
def get_status():
    """Get current system status."""
    _sync_transfer_state()
    return jsonify({
        'transfer': transfer_state,
        'servers': _server_status(),
        'events': event_log[-50:],
        'bridge': _bridge_status(),
    })


@app.route('/api/metrics')
def get_metrics():
    """Get performance metrics."""
    _sync_transfer_state()
    return jsonify({
        'throughput': transfer_state.get('throughput', 0),
        'recovery_time': transfer_state.get('recovery_time'),
        'recovery_kind': transfer_state.get('recovery_kind'),
        'recovery_target': transfer_state.get('recovery_target'),
        'progress': transfer_state.get('progress', 0),
    })


@app.route('/api/storage', methods=['GET'])
def get_storage():
    """Per-server storage quota and usage, recomputed from disk."""
    return jsonify(_storage_snapshot())


@app.route('/api/storage', methods=['POST'])
def set_storage():
    """Set a server's storage quota (MB). host in {h3, h4}, quota_mb >= 0 (0 = unlimited)."""
    data = request.get_json(force=True, silent=True) or {}
    host = data.get('host')
    if host not in ('h3', 'h4'):
        return jsonify({'error': 'Invalid host: must be h3 or h4'}), 400
    try:
        quota_mb = int(data.get('quota_mb', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'quota_mb must be an integer'}), 400
    if quota_mb < 0:
        return jsonify({'error': 'quota_mb must be >= 0'}), 400
    quotas = _load_storage_quotas()
    quotas[host] = quota_mb
    _save_storage_quotas(quotas)
    log_event(f'[STORAGE] Quota set for {host}: {quota_mb} MB (0 = unlimited)')
    return jsonify({'host': host, 'quota_mb': quota_mb,
                    'used_mb': _used_mb(host)})


@socketio.on('connect')
def handle_connect():
    emit('event', {'message': 'Connected to dashboard', 'timestamp': time.strftime('%H:%M:%S')})


if __name__ == '__main__':
    log_event('Dashboard starting on http://localhost:5000')
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
