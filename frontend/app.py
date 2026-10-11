"""
Flask web dashboard for the Distributed Backup System.
Serves a single-page app with live topology, transfer control, metrics, and event log.
"""
import json
import os
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
STORED_DIR = os.path.join(BASE_DIR, 'stored_files')
SHARED_DIR = os.path.join(BASE_DIR, 'shared_files')
REQUEST_FILE = os.path.join(SHARED_DIR, 'transfer_request.json')
RESULT_FILE = os.path.join(SHARED_DIR, 'transfer_result.json')
PROGRESS_FILE = os.path.join(SHARED_DIR, 'transfer_progress.json')
EVENTS_FILE = os.path.join(SHARED_DIR, 'transfer_events.json')
CANCEL_FILE = os.path.join(SHARED_DIR, 'transfer_cancel.json')
LINK_REQUEST_FILE = os.path.join(SHARED_DIR, 'link_request.json')
LINK_RESULT_FILE = os.path.join(SHARED_DIR, 'link_result.json')

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


def _sync_transfer_state():
    """Fold the bridge's progress/result/events into dashboard state.

    The bridge is a separate process writing plain files, so these are the only
    channel the dashboard has to a running transfer. Keep this tolerant: a
    partially written or missing file must not take the endpoint down.
    """
    progress = _read_json(PROGRESS_FILE)
    if _matches_current(progress):
        transfer_state['running'] = bool(progress.get('running'))
        transfer_state['chunks_sent'] = progress.get('chunk', 0)
        transfer_state['total_chunks'] = progress.get('total', 0)
        transfer_state['throughput'] = progress.get('throughput', 0.0)
        if not progress.get('running') and not progress.get('cancelled'):
            transfer_state['progress'] = 100

    result = _read_json(RESULT_FILE)
    if _matches_current(result):
        # Stamp the first time we see this result so the UI can show how
        # long the completed state has been held, rather than looking stuck.
        if transfer_state.get('result') != result:
            transfer_state['completed_at'] = time.strftime('%H:%M:%S')
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
        _events_ingested = len(events)
        # Keep a generous tail; the dashboard renders the most recent ones.
        del event_log[:-EVENT_LOG_LIMIT]


def log_event(message):
    """Add an event to the log and broadcast via WebSocket."""
    timestamp = time.strftime('%H:%M:%S')
    entry = f'[{timestamp}] {message}'
    event_log.append(entry)
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
                for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))
            ]
        else:
            files[server] = []
    return jsonify(files)


@app.route('/api/browse')
def browse_files():
    """List files in shared_files/ for the dashboard file picker."""
    os.makedirs(SHARED_DIR, exist_ok=True)
    files = [
        {'name': f, 'size': os.path.getsize(os.path.join(SHARED_DIR, f))}
        for f in sorted(os.listdir(SHARED_DIR))
        if os.path.isfile(os.path.join(SHARED_DIR, f))
        and not f.startswith('transfer_')
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
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': 'Invalid filename'}), 400

    os.makedirs(SHARED_DIR, exist_ok=True)
    src = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(src):
        return jsonify({'error': f'File not found in shared_files/: {filename}'}), 404

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
                                          'request_id': request_id})

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
        log_event(f'{verb}: {sw1} <-> {sw2} confirmed by Mininet')
        socketio.emit('link_event', {'sw1': sw1, 'sw2': sw2, 'status': state})
        return jsonify({'status': state, 'sw1': sw1, 'sw2': sw2, 'applied': True})
    if confirmed:
        log_event(f'{verb}: {sw1} <-> {sw2} REJECTED — {confirmed.get("error")}')
        return jsonify({'status': 'error', 'sw1': sw1, 'sw2': sw2,
                        'applied': False, 'error': confirmed.get('error')}), 502

    log_event(f'{verb}: {sw1} <-> {sw2} requested but Mininet did not confirm '
              f'(is mininet/topology.py running?)')
    return jsonify({'status': 'unconfirmed', 'sw1': sw1, 'sw2': sw2,
                    'applied': False,
                    'error': 'Mininet did not confirm within 5s — is '
                             'mininet/topology.py running?'}), 504


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
        'progress': transfer_state.get('progress', 0),
    })


@socketio.on('connect')
def handle_connect():
    emit('event', {'message': 'Connected to dashboard', 'timestamp': time.strftime('%H:%M:%S')})


if __name__ == '__main__':
    log_event('Dashboard starting on http://localhost:5000')
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
