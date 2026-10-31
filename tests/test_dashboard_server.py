"""
Dashboard server-control routes and recovery state — no Mininet needed.
"""

import importlib.util
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')


def _load_app():
    spec = importlib.util.spec_from_file_location('dashboard_server_under_test',
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
    mod.SERVER_REQUEST_FILE = os.path.join(shared, 'server_request.json')
    mod.SERVER_RESULT_FILE = os.path.join(shared, 'server_result.json')
    mod.SERVER_STATUS_FILE = os.path.join(shared, 'server_status.json')
    mod.transfer_state.update({
        'running': False, 'filename': None, 'request_id': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'recovery_kind': None,
        'recovery_target': None, 'server_down_at': None, 'phase': None,
        'attempt': 0, 'max_attempts': 2, 'resume_from': 0, 'server': None,
        'result': None, 'completed_at': None,
    })
    mod.event_log.clear()
    mod._events_ingested = 0
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def _answer(mod):
    for _ in range(200):
        if os.path.exists(mod.SERVER_REQUEST_FILE):
            with open(mod.SERVER_REQUEST_FILE) as f:
                req = json.load(f)
            result = {
                'host': req['host'], 'action': req['action'],
                'request_id': req['request_id'], 'applied': True,
                'state': 'down' if req['action'] == 'down' else 'up',
                'message': 'ok',
            }
            with open(mod.SERVER_RESULT_FILE, 'w') as f:
                json.dump(result, f)
            return
        time.sleep(0.01)


def test_server_down_writes_request_and_confirms(dash):
    threading.Thread(target=_answer, args=(dash,), daemon=True).start()
    response = dash.app.test_client().post('/api/server-down', json={'host': 'h3'})
    assert response.status_code == 200
    assert response.get_json()['applied'] is True
    assert any('[SERVER] h3 down confirmed' in e for e in dash.event_log)


def test_invalid_host_is_rejected(dash):
    response = dash.app.test_client().post('/api/server-down', json={'host': 'h1'})
    assert response.status_code == 400


def test_unconfirmed_server_request_is_504(dash, monkeypatch):
    monkeypatch.setattr(dash.time, 'sleep', lambda seconds: None)
    response = dash.app.test_client().post('/api/server-up', json={'host': 'h4'})
    assert response.status_code == 504


def test_status_exposes_server_liveness(dash):
    with open(dash.SERVER_STATUS_FILE, 'w') as f:
        json.dump({'checked_at': '12:00:00',
                   'h3': {'alive': False, 'pid': 1},
                   'h4': {'alive': True, 'pid': 2}}, f)
    data = dash.app.test_client().get('/api/status').get_json()
    assert data['servers']['h3']['alive'] is False
    assert data['servers']['h4']['alive'] is True


def test_status_exposes_retry_phase(dash):
    dash.transfer_state.update({'filename': 'a.bin', 'request_id': 'r1',
                                'running': True, 'start_time': time.time()})
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': 'r1', 'running': True,
                   'chunk': 2, 'total': 4, 'throughput': 0.0,
                   'phase': 'retrying', 'attempt': 1, 'max_attempts': 2,
                   'resume_from': 2, 'server': 'h3'}, f)
    state = dash.app.test_client().get('/api/status').get_json()['transfer']
    assert state['phase'] == 'retrying'
    assert state['attempt'] == 1
    assert state['resume_from'] == 2
    assert state['server'] == 'h3'
    assert state['server_down_at'] is not None


def test_server_recovery_is_labeled(dash):
    dash.transfer_state.update({'filename': 'a.bin', 'request_id': 'r1',
                                'running': True, 'start_time': time.time()})
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': 'r1', 'running': True,
                   'chunk': 2, 'total': 4, 'throughput': 0.0,
                   'phase': 'retrying', 'attempt': 1, 'max_attempts': 2,
                   'resume_from': 2, 'server': 'h3'}, f)
    dash.app.test_client().get('/api/status')
    dash.transfer_state['server_down_at'] = time.time() - 5
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'request_id': 'r1', 'running': True,
                   'chunk': 3, 'total': 4, 'throughput': 1.0,
                   'phase': 'resuming', 'attempt': 2, 'max_attempts': 2,
                   'resume_from': 2, 'server': 'h3'}, f)
    state = dash.app.test_client().get('/api/status').get_json()['transfer']
    assert state['recovery_time'] >= 5
    assert state['recovery_kind'] == 'server'
    assert state['recovery_target'] == 'h3'
    assert any('[RECOVERY] Server h3' in e for e in dash.event_log)


def test_metrics_expose_recovery_kind(dash):
    dash.transfer_state.update({'recovery_time': 3.2, 'recovery_kind': 'server',
                                'recovery_target': 'h3'})
    data = dash.app.test_client().get('/api/metrics').get_json()
    assert data['recovery_time'] == 3.2
    assert data['recovery_kind'] == 'server'
    assert data['recovery_target'] == 'h3'
