"""
Local end-to-end chain: real server + real bridge loop + real Flask dashboard.
Everything except Mininet/Ryu runs here.

Pins the reported symptom — "new selection never works; the file lands in
storage but the UI says fail" — by driving two consecutive transfers and a
double click through the actual processes and files, and asserting the UI ends
on the right run's result every time.
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


@pytest.fixture()
def chain(tmp_path):
    """A live chain: backup server thread + bridge thread + Flask test client."""
    from server.server import BackupServer

    shared = tmp_path / 'shared'
    shared.mkdir()
    store = tmp_path / 'store'
    store.mkdir()
    payload_a = b'A' * 24576  # 24 KB = 3 chunks
    payload_b = b'B' * 10240  # 10 KB = 2 chunks
    (shared / 'a.bin').write_bytes(payload_a)
    (shared / 'b.bin').write_bytes(payload_b)

    port = free_port()
    srv = BackupServer(host='127.0.0.1', port=port, storage_dir=str(store))
    server_thread = threading.Thread(target=srv.start, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    bridge = _load('bridge_e2e_under_test', BRIDGE_SPEC)
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

    dash = _load('dashboard_e2e_under_test', APP_SPEC)
    dash.SHARED_DIR = str(shared)
    dash.REQUEST_FILE = bridge.REQUEST_FILE
    dash.RESULT_FILE = bridge.RESULT_FILE
    dash.PROGRESS_FILE = bridge.PROGRESS_FILE
    dash.EVENTS_FILE = bridge.EVENTS_FILE
    dash.CANCEL_FILE = bridge.CANCEL_FILE
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
           'payload_a': payload_a, 'payload_b': payload_b}
    srv.stop()


def test_sequential_transfers_both_settle_on_their_own_result(chain):
    """A then B: each click ends showing its own run's done — never a stale one."""
    client = chain['client']
    first = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert first.status_code == 200
    id_a = first.get_json()['request_id']

    res_a = _wait_result(chain['bridge'].RESULT_FILE)
    assert res_a['status'] == 'done', res_a
    assert res_a['request_id'] == id_a
    assert (chain['store'] / 'a.bin').read_bytes() == chain['payload_a']

    state = client.get('/api/status').get_json()
    assert state['transfer']['result']['status'] == 'done'
    assert state['transfer']['result']['request_id'] == id_a
    assert state['transfer']['running'] is False
    assert state['bridge']['alive'] is True
    # Final metrics survive completion: 24 KB = 3 chunks, throughput measured.
    assert state['transfer']['chunks_sent'] == 3
    assert state['transfer']['total_chunks'] == 3
    assert client.get('/api/metrics').get_json()['throughput'] > 0

    second = client.post('/api/start-transfer', json={'filename': 'b.bin'})
    assert second.status_code == 200
    id_b = second.get_json()['request_id']
    assert id_b != id_a

    res_b = _wait_result(chain['bridge'].RESULT_FILE)
    assert res_b['status'] == 'done', res_b
    assert res_b['request_id'] == id_b
    assert (chain['store'] / 'b.bin').read_bytes() == chain['payload_b']

    state = client.get('/api/status').get_json()
    assert state['transfer']['result']['request_id'] == id_b


def test_double_click_yields_one_run_and_one_rejection(chain):
    """Two instant clicks: exactly one transfer runs, the other gets 409."""
    client = chain['client']
    r1 = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    r2 = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]

    winner = r1 if r1.status_code == 200 else r2
    res = _wait_result(chain['bridge'].RESULT_FILE)
    assert res['status'] == 'done', res
    assert res['request_id'] == winner.get_json()['request_id']
    assert (chain['store'] / 'a.bin').read_bytes() == chain['payload_a']
