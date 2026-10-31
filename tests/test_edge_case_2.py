"""
Edge case 2: sender crashes mid-transfer.

Tests that an interrupted transfer leaves a .part file on the server,
that /api/partial-files lists it, that /api/start-transfer passes
resume_from through to the bridge, and that a real client can resume
from the partial and complete the transfer.
"""

import importlib.util
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

BRIDGE_SPEC = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')
APP_SPEC = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')


def free_port():
    import socket as s
    with s.socket(s.AF_INET, s.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wait_result(path, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path) as f:
                res = json.load(f)
            if res.get('status') in ('done', 'failed', 'stopped'):
                return res
        except Exception:
            pass
        time.sleep(0.2)
    raise TimeoutError(f'no terminal result in {path} within {timeout}s')


# ---------------------------------------------------------------------------
# Backend unit tests (Flask app only, no Mininet)
# ---------------------------------------------------------------------------

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')


def _load_app():
    spec = importlib.util.spec_from_file_location('edge2_dashboard_under_test',
                                                  SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def dash(tmp_path):
    mod = _load_app()
    shared = str(tmp_path / 'shared')
    os.makedirs(shared, exist_ok=True)
    mod.SHARED_DIR = shared
    mod.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    mod.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    mod.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    mod.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    mod.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    mod.STORED_DIR = str(tmp_path / 'stored')
    for server in ('h3', 'h4'):
        os.makedirs(os.path.join(mod.STORED_DIR, server), exist_ok=True)
    mod.transfer_state.update({
        'running': False, 'filename': None, 'request_id': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'result': None,
        'completed_at': None,
    })
    mod.event_log.clear()
    mod._events_ingested = 0
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def test_partial_files_endpoint_lists_part_files(dash):
    """Interrupted (.part) transfers on h3/h4 are listed with chunk counts."""
    client = dash.app.test_client()
    from common.protocol import CHUNK_SIZE
    # Create .part files on both servers.
    with open(os.path.join(dash.STORED_DIR, 'h3', 'big.bin.part'), 'wb') as f:
        f.write(b'P' * (CHUNK_SIZE * 2 + 100))   # 2 full chunks + partial
    with open(os.path.join(dash.STORED_DIR, 'h4', 'small.bin.part'), 'wb') as f:
        f.write(b'Q' * CHUNK_SIZE)                 # 1 full chunk
    # A non-.part file must NOT appear.
    with open(os.path.join(dash.STORED_DIR, 'h3', 'done.bin'), 'wb') as f:
        f.write(b'D' * 50)

    data = client.get('/api/partial-files').get_json()
    names_h3 = {p['name'] for p in data['h3']}
    names_h4 = {p['name'] for p in data['h4']}
    assert 'big.bin' in names_h3
    assert 'small.bin' in names_h4
    assert 'done.bin' not in names_h3
    big = [p for p in data['h3'] if p['name'] == 'big.bin'][0]
    assert big['chunks'] == 2
    assert big['partial_name'] == 'big.bin.part'


def test_start_transfer_passes_resume_from(dash):
    """resume_from in the POST body is written to the request file."""
    client = dash.app.test_client()
    resp = client.post('/api/start-transfer',
                       json={'filename': 'a.bin', 'resume_from': 5})
    assert resp.status_code == 200
    assert resp.get_json()['request_id']
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['resume_from'] == 5
    assert req['filename'] == 'a.bin'


def test_start_transfer_defaults_resume_from_to_zero(dash):
    """Without resume_from, the request file has resume_from 0."""
    client = dash.app.test_client()
    client.post('/api/start-transfer', json={'filename': 'a.bin'})
    with open(dash.REQUEST_FILE) as f:
        req = json.load(f)
    assert req['resume_from'] == 0


# ---------------------------------------------------------------------------
# End-to-end test: sender crash mid-transfer, then resume
# ---------------------------------------------------------------------------

@pytest.fixture()
def chain(tmp_path):
    """A live chain: backup server thread + bridge loop + Flask dashboard."""
    from server.server import BackupServer

    shared = tmp_path / 'shared'
    shared.mkdir()
    store = tmp_path / 'store'
    store.mkdir()
    payload_a = b'A' * 24576  # 24 KB = 3 chunks
    (shared / 'a.bin').write_bytes(payload_a)

    port = free_port()
    srv = BackupServer(host='127.0.0.1', port=port, storage_dir=str(store))
    server_thread = threading.Thread(target=srv.start, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    bridge = _load('bridge_edge2_under_test', BRIDGE_SPEC)
    bridge.SHARED_DIR = str(shared)
    bridge.REQUEST_FILE = str(shared / 'transfer_request.json')
    bridge.RESULT_FILE = str(shared / 'transfer_result.json')
    bridge.PROGRESS_FILE = str(shared / 'transfer_progress.json')
    bridge.EVENTS_FILE = str(shared / 'transfer_events.json')
    bridge.CANCEL_FILE = str(shared / 'transfer_cancel.json')
    bridge.LOCK_FILE = str(shared / 'bridge.lock')
    bridge.ALIVE_FILE = str(shared / 'bridge_alive.json')
    bridge.SERVERS = [f'127.0.0.1:{port}']
    bridge.POLL_INTERVAL = 0.05
    bridge.CLIENT_TIMEOUT = 60
    bridge_thread = threading.Thread(target=bridge.main, daemon=True)
    bridge_thread.start()
    time.sleep(0.5)

    dash = _load('dashboard_edge2_under_test', APP_SPEC)
    dash.SHARED_DIR = str(shared)
    dash.REQUEST_FILE = bridge.REQUEST_FILE
    dash.RESULT_FILE = bridge.RESULT_FILE
    dash.PROGRESS_FILE = bridge.PROGRESS_FILE
    dash.EVENTS_FILE = bridge.EVENTS_FILE
    dash.CANCEL_FILE = bridge.CANCEL_FILE
    dash.SERVER_REQUEST_FILE = str(shared / 'server_request.json')
    dash.SERVER_RESULT_FILE = str(shared / 'server_result.json')
    dash.SERVER_STATUS_FILE = str(shared / 'server_status.json')
    dash.STORED_DIR = str(store)
    dash.transfer_state.update({
        'running': False, 'filename': None, 'request_id': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'result': None,
        'completed_at': None,
    })
    dash.event_log.clear()
    dash._events_ingested = 0

    yield {'client': dash.app.test_client(), 'dash': dash,
           'bridge': bridge, 'shared': shared, 'store': store,
           'payload_a': payload_a}
    srv.stop()


def test_sender_crash_resume_e2e(tmp_path):
    """
    Sender crashes after chunk 0 (leaving a .part file), then resumes
    from chunk 1 and completes the transfer.
    """
    import socket
    from common.protocol import (CHUNK_SIZE, MSG_HELLO, MSG_DATA, MSG_ACK,
                                  pack_hello, pack_data, pack_ack, unpack_ack)
    from common.transport import recv_frame
    from common.checksum import compute_crc32
    from server.server import BackupServer

    shared = tmp_path / 'shared'
    shared.mkdir()
    store = tmp_path / 'store'
    store.mkdir()
    content = b'A' * 24576  # 24 KB = 3 chunks
    (shared / 'a.bin').write_bytes(content)
    total_chunks = (len(content) + CHUNK_SIZE - 1) // CHUNK_SIZE  # 3

    port = free_port()

    # --- Phase 1: sender crashes after chunk 0 ---
    fake_ready = threading.Event()

    def fake_server_then_die():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', port))
        srv.listen(1)
        srv.settimeout(10)
        fake_ready.set()
        conn, _ = srv.accept()
        conn.settimeout(10)
        try:
            hello = recv_frame(conn)           # HELLO
            assert hello is not None and hello['type'] == MSG_HELLO
            conn.sendall(pack_ack(0))          # ready
            data = recv_frame(conn)            # chunk 0
            assert data is not None
            # Write chunk 0 to .part, then die (sender crash).
            with open(store / 'a.bin.part', 'wb') as f:
                f.write(data['payload'])
            conn.sendall(pack_ack(data['index']))
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=fake_server_then_die, daemon=True)
    t.start()
    assert fake_ready.wait(5)

    bridge = _load('bridge_edge2_crash_under_test', BRIDGE_SPEC)
    bridge.SHARED_DIR = str(shared)
    bridge.REQUEST_FILE = str(shared / 'transfer_request.json')
    bridge.RESULT_FILE = str(shared / 'transfer_result.json')
    bridge.PROGRESS_FILE = str(shared / 'transfer_progress.json')
    bridge.EVENTS_FILE = str(shared / 'transfer_events.json')
    bridge.CANCEL_FILE = str(shared / 'transfer_cancel.json')
    bridge.LOCK_FILE = str(shared / 'bridge.lock')
    bridge.ALIVE_FILE = str(shared / 'bridge_alive.json')
    bridge.SERVERS = [f'127.0.0.1:{port}']
    bridge.POLL_INTERVAL = 0.05
    bridge.CLIENT_TIMEOUT = 30

    try:
        res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r1'})
    finally:
        t.join(timeout=5)

    # First attempt should fail (client crashed after chunk 0).
    assert res['status'] == 'failed', res
    assert res['resume_point'] == 1, f"expected resume_point=1, got {res.get('resume_point')}"

    # --- Phase 2: resume from chunk 1 ---
    real = BackupServer(host='127.0.0.1', port=port, storage_dir=str(store))
    real_thread = threading.Thread(target=real.start, daemon=True)
    real_thread.start()
    time.sleep(0.5)

    try:
        res2 = bridge.process_request({'filename': 'a.bin', 'request_id': 'r2',
                                       'resume_from': 1})
    finally:
        real.stop()
        real_thread.join(timeout=5)

    assert res2['status'] == 'done', res2
    assert res2['resumed_from'] == 1, f"expected resumed_from=1, got {res2.get('resumed_from')}"

    # Final file must be complete and correct.
    assert (store / 'a.bin').read_bytes() == content
    # No .part file should remain.
    assert not (store / 'a.bin.part').exists()
    assert (store / 'a.bin').read_bytes() == content
    # No .part file should remain.
    assert not (store / 'a.bin.part').exists()


def test_start_transfer_without_resume_from_starts_fresh(chain):
    """Without resume_from, a fresh transfer overwrites any partial."""
    import socket
    from common.protocol import (CHUNK_SIZE, MSG_HELLO, MSG_DATA, MSG_ACK,
                                  pack_hello, pack_data, pack_ack, unpack_ack)
    from common.transport import recv_frame

    client = chain['client']
    bridge = chain['bridge']
    store = chain['store']
    content = chain['payload_a']
    total_chunks = (len(content) + CHUNK_SIZE - 1) // CHUNK_SIZE

    port = int(bridge.SERVERS[0].split(':')[1])

    # Leave a stale .part file.
    with open(store / 'a.bin.part', 'wb') as f:
        f.write(b'X' * CHUNK_SIZE)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', port))
    srv.listen(1)
    srv.settimeout(10)

    def fresh_server():
        conn, _ = srv.accept()
        conn.settimeout(10)
        try:
            hello = recv_frame(conn)
            assert hello is not None and hello['type'] == MSG_HELLO
            # resume_from=0 -> server truncates, starts fresh.
            assert unpack_ack(recv_frame(conn)['payload']) == 0
            for i in range(total_chunks):
                data = recv_frame(conn)
                assert data is not None
                conn.sendall(pack_ack(data['index']))
        finally:
            conn.close()
            srv.close()

    t = threading.Thread(target=fresh_server, daemon=True)
    t.start()
    try:
        res = bridge.process_request({'filename': 'a.bin', 'request_id': 'r2'})
    finally:
        t.join(timeout=5)

    assert res['status'] == 'done', res
    assert (store / 'a.bin').read_bytes() == content
