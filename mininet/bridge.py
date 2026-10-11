"""
Bridge process — runs on h1 inside Mininet.

Watches shared_files/transfer_request.json (inside Mininet: /tmp/shared/).
When the dashboard writes a request, runs the TCP client against the backup
servers, streams its progress into transfer_progress.json + transfer_events.json,
and writes transfer_result.json when it finishes.

A transfer_cancel.json makes it kill a running client, so Stop really stops.
"""

import json
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
CLIENT_SCRIPT = os.path.join(BASE_DIR, 'client', 'client.py')

# Inside Mininet, shared_files/ is bind-mounted at /tmp/shared/.
# Outside Mininet (host testing), fall back to the repo directory.
SHARED_DIR = os.environ.get(
    'SHARED_DIR',
    '/tmp/shared' if os.path.isdir('/tmp/shared') else os.path.join(BASE_DIR, 'shared_files')
)
REQUEST_FILE = os.path.join(SHARED_DIR, 'transfer_request.json')
RESULT_FILE = os.path.join(SHARED_DIR, 'transfer_result.json')
PROGRESS_FILE = os.path.join(SHARED_DIR, 'transfer_progress.json')
EVENTS_FILE = os.path.join(SHARED_DIR, 'transfer_events.json')
CANCEL_FILE = os.path.join(SHARED_DIR, 'transfer_cancel.json')

SERVERS = os.environ.get('BACKUP_SERVERS', '10.0.0.3:9000,10.0.0.4:9000').split(',')
POLL_INTERVAL = float(os.environ.get('BRIDGE_POLL_INTERVAL', '0.5'))
CLIENT_TIMEOUT = float(os.environ.get('BRIDGE_TIMEOUT', '300'))


def _write_json(path, data):
    """Write JSON atomically so a reader never sees a half-written file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def emit_event(message):
    """Append an event line for the dashboard to pick up."""
    entry = f'[{time.strftime("%H:%M:%S")}] {message}'
    lines = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE) as f:
                lines = json.load(f)
        except Exception:
            lines = []
    lines.append(entry)
    _write_json(EVENTS_FILE, lines[-50:])


def clear_transfer_files():
    for p in (REQUEST_FILE, RESULT_FILE, PROGRESS_FILE, EVENTS_FILE, CANCEL_FILE):
        if os.path.exists(p):
            os.unlink(p)


def run_client(filename, filepath):
    """Run the client, streaming progress. Returns (status, error)."""
    cmd = [sys.executable, CLIENT_SCRIPT, filepath, '--servers'] + SERVERS
    print(f"[BRIDGE] Running: {' '.join(cmd)}", flush=True)
    emit_event(f'Client started on h1 for {filename}')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    stored = None
    completed = False
    last_error = None
    deadline = time.time() + CLIENT_TIMEOUT

    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            print(line, flush=True)
            if line.startswith('[PROGRESS]'):
                try:
                    p = json.loads(line[len('[PROGRESS]'):].strip())
                    p.update({'filename': filename, 'running': True})
                    _write_json(PROGRESS_FILE, p)
                except Exception:
                    pass
            elif line.startswith('[STORED]'):
                try:
                    stored = json.loads(line[len('[STORED]'):].strip())
                except Exception:
                    pass
            elif line.startswith('[COMPLETE]'):
                completed = True
                # The client's final tally — the count of chunks the server ACKed.
                try:
                    done = json.loads(line[len('[COMPLETE]'):].strip())
                    emit_event(f"Server ACKed {done.get('chunks')} chunks "
                               f"({done.get('bytes')} bytes) at {done.get('throughput')} KB/s")
                except Exception:
                    pass
            elif line.startswith('[CLIENT]') and not line.startswith('[CLIENT] Progress'):
                # Stage changes only. Progress lines are carried by transfer_progress.json;
                # emitting them as events buries the events that actually matter.
                # Keep the last failure line so the dashboard can show why it failed
                # instead of an opaque "client exited 1".
                emit_event(line)
                if 'fail' in line.lower() or 'error' in line.lower() or 'lost' in line.lower():
                    last_error = line
            if os.path.exists(CANCEL_FILE):
                emit_event(f'Stop requested — killing client for {filename}')
                proc.send_signal(signal.SIGTERM)
                break
            if time.time() > deadline:
                emit_event(f'Client timed out after {CLIENT_TIMEOUT:.0f}s')
                proc.kill()
                break
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if os.path.exists(CANCEL_FILE):
        os.unlink(CANCEL_FILE)
        _write_json(PROGRESS_FILE, {'filename': filename, 'running': False, 'cancelled': True})
        return 'stopped', 'Cancelled by user'

    if stored:
        emit_event(f"Server stored {stored['filename']} "
                   f"({stored.get('size', 0)} bytes, {stored.get('chunks', 0)} chunks)")

    if proc.returncode == 0 and completed:
        # Exit code 0 alone is not proof: a client that never connected still
        # exits 0, and reporting that as "done" is how a failed transfer was
        # previously shown as a success. Require the [COMPLETE] tally too.
        emit_event(f'Client finished {filename}')
        return 'done', None

    reason = last_error or (f'client exited {proc.returncode}'
                            if proc.returncode != 0
                            else 'client exited without completing a transfer')
    if not reason.startswith('[CLIENT]'):
        reason = f'[CLIENT] {reason}'
    return 'failed', reason[:300]


def process_request(request):
    """Run the transfer for the requested file. Returns a result dict."""
    filename = request.get('filename', '')
    if not filename or '..' in filename or '/' in filename or '\\' in filename:
        return {'filename': filename, 'status': 'failed', 'error': 'Invalid filename'}

    filepath = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(filepath):
        return {'filename': filename, 'status': 'failed', 'error': 'File not found'}

    clear_transfer_files()
    try:
        status, error = run_client(filename, filepath)
    except Exception as e:
        return {'filename': filename, 'status': 'failed', 'error': str(e)}

    result = {'filename': filename, 'status': status}
    if error:
        result['error'] = error
    return result


def main():
    print(f"[BRIDGE] Watching {REQUEST_FILE}", flush=True)
    print(f"[BRIDGE] Servers: {SERVERS}", flush=True)
    emit_event('Bridge started on h1')
    while True:
        try:
            if os.path.isfile(REQUEST_FILE):
                with open(REQUEST_FILE, 'r') as f:
                    request = json.load(f)
                os.unlink(REQUEST_FILE)
                result = process_request(request)
                result['timestamp'] = time.strftime('%H:%M:%S')
                _write_json(RESULT_FILE, result)
                emit_event(
                    f"Transfer {result['status']}: {result['filename']}"
                    + (f" — {result.get('error')}" if result.get('error') else '')
                )
                print(f"[BRIDGE] Result: {result}", flush=True)
        except Exception as e:
            print(f"[BRIDGE] Error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
