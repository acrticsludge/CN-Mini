"""
Flask web dashboard for the Distributed Backup System.
Serves a single-page app with live topology, transfer control, metrics, and event log.
"""
import json
import os
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

# Global state
transfer_state = {
    'running': False,
    'filename': None,
    'progress': 0,
    'chunks_sent': 0,
    'total_chunks': 0,
    'throughput': 0.0,
    'start_time': None,
    'recovery_time': None,
    'result': None,
}

event_log = []


def _sync_transfer_state():
    """Fold the bridge's result into transfer_state so the dashboard can show progress."""
    try:
        if os.path.exists(RESULT_FILE):
            with open(RESULT_FILE) as f:
                result = json.load(f)
            if result.get('filename') == transfer_state['filename']:
                transfer_state['running'] = False
                if result.get('status') == 'done':
                    transfer_state['progress'] = 100
                transfer_state['result'] = result
    except Exception:
        pass


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

    if os.path.exists(RESULT_FILE):
        os.unlink(RESULT_FILE)
    with open(REQUEST_FILE, 'w') as f:
        json.dump({'filename': filename}, f)

    transfer_state['running'] = True
    transfer_state['filename'] = filename
    transfer_state['progress'] = 0
    transfer_state['chunks_sent'] = 0
    transfer_state['start_time'] = time.time()

    log_event(f'Transfer queued: {filename} (bridge will pick it up)')
    socketio.emit('transfer_state', transfer_state)
    return jsonify({'status': 'queued', 'filename': filename})


@app.route('/api/stop-transfer', methods=['POST'])
def stop_transfer():
    """Stop the current transfer."""
    transfer_state['running'] = False
    log_event('Transfer stopped by user')
    socketio.emit('transfer_state', transfer_state)
    return jsonify({'status': 'stopped'})


@app.route('/api/link-down', methods=['POST'])
def link_down():
    """Simulate a link failure between two switches."""
    data = request.get_json()
    sw1 = data.get('sw1', 's1')
    sw2 = data.get('sw2', 's2')

    log_event(f'Link failure: {sw1} <-> {sw2}')
    socketio.emit('link_event', {'sw1': sw1, 'sw2': sw2, 'status': 'down'})
    return jsonify({'status': 'link_down', 'sw1': sw1, 'sw2': sw2})


@app.route('/api/link-up', methods=['POST'])
def link_up():
    """Restore a link between two switches."""
    data = request.get_json()
    sw1 = data.get('sw1', 's1')
    sw2 = data.get('sw2', 's2')

    log_event(f'Link restored: {sw1} <-> {sw2}')
    socketio.emit('link_event', {'sw1': sw1, 'sw2': sw2, 'status': 'up'})
    return jsonify({'status': 'link_up', 'sw1': sw1, 'sw2': sw2})


@app.route('/api/status')
def get_status():
    """Get current system status."""
    _sync_transfer_state()
    return jsonify({
        'transfer': transfer_state,
        'events': event_log[-20:],
    })


@app.route('/api/metrics')
def get_metrics():
    """Get performance metrics."""
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
