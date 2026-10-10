"""
Flask web dashboard for the Distributed Backup System.
Serves a single-page app with live topology, transfer control, metrics, and event log.
"""
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
}

event_log = []


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


@app.route('/api/start-transfer', methods=['POST'])
def start_transfer():
    """Start a file transfer from client to server."""
    data = request.get_json()
    filename = data.get('filename', 'test_file.bin')
    file_path = data.get('file_path', os.path.join(BASE_DIR, 'test_file.bin'))

    # Validate file path to prevent directory traversal
    real_path = os.path.realpath(file_path)
    if '..' in file_path or not real_path.startswith(os.path.realpath(BASE_DIR)):
        return jsonify({'error': 'Invalid file path'}), 400

    transfer_state['running'] = True
    transfer_state['filename'] = filename
    transfer_state['progress'] = 0
    transfer_state['chunks_sent'] = 0
    transfer_state['start_time'] = time.time()

    log_event(f'Starting transfer: {filename}')
    socketio.emit('transfer_state', transfer_state)
    return jsonify({'status': 'started', 'filename': filename})


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
