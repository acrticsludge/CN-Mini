"""
Bridge process — runs on h1 inside Mininet.

Watches /tmp/shared/transfer_request.json (host: shared_files/).
When the dashboard writes a request, runs the TCP client against
the backup servers and writes transfer_result.json back.
"""

import json
import os
import subprocess
import sys
import time

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
CLIENT_SCRIPT = os.path.join(BASE_DIR, 'client', 'client.py')

# Inside Mininet, shared_files/ is bind-mounted at /tmp/shared/.
# Outside Mininet (host testing), fall back to the repo directory.
SHARED_DIR = '/tmp/shared' if os.path.isdir('/tmp/shared') else os.path.join(BASE_DIR, 'shared_files')
REQUEST_FILE = os.path.join(SHARED_DIR, 'transfer_request.json')
RESULT_FILE = os.path.join(SHARED_DIR, 'transfer_result.json')

SERVERS = os.environ.get('BACKUP_SERVERS', '10.0.0.3:9000,10.0.0.4:9000').split(',')
POLL_INTERVAL = float(os.environ.get('BRIDGE_POLL_INTERVAL', '1.0'))


def process_request(request):
    """Run the client for the requested file. Returns a result dict."""
    filename = request.get('filename', '')
    if not filename or '..' in filename or '/' in filename:
        return {'filename': filename, 'status': 'failed', 'error': 'Invalid filename'}

    filepath = os.path.join(SHARED_DIR, filename)
    if not os.path.isfile(filepath):
        return {'filename': filename, 'status': 'failed', 'error': 'File not found'}

    cmd = [sys.executable, CLIENT_SCRIPT, filepath, '--servers'] + SERVERS
    print(f"[BRIDGE] Running: {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        print(proc.stdout, flush=True)
        if proc.returncode == 0:
            return {'filename': filename, 'status': 'done'}
        return {'filename': filename, 'status': 'failed', 'error': proc.stderr[-500:]}
    except subprocess.TimeoutExpired:
        return {'filename': filename, 'status': 'failed', 'error': 'Transfer timed out'}
    except Exception as e:
        return {'filename': filename, 'status': 'failed', 'error': str(e)}


def main():
    print(f"[BRIDGE] Watching {REQUEST_FILE}", flush=True)
    while True:
        try:
            if os.path.isfile(REQUEST_FILE):
                with open(REQUEST_FILE, 'r') as f:
                    request = json.load(f)
                os.unlink(REQUEST_FILE)
                result = process_request(request)
                result['timestamp'] = time.strftime('%H:%M:%S')
                with open(RESULT_FILE, 'w') as f:
                    json.dump(result, f)
                print(f"[BRIDGE] Result: {result}", flush=True)
        except Exception as e:
            print(f"[BRIDGE] Error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
