"""
Dashboard busy-guard + metrics-sync regressions — no Mininet needed.

Pins two behaviours behind the "works 10% of clicks / metrics always zero"
reports:
  1. A second POST while the bridge still has work must be rejected (409), not
     silently overwrite the live run's request/progress/result.
  2. /api/metrics must reflect the bridge's latest progress, not stale state.
"""

import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'app.py')


def _load_app():
    spec = importlib.util.spec_from_file_location('dashboard_under_test', SPEC_PATH)
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
    mod.transfer_state.update({
        'running': False, 'filename': None, 'progress': 0,
        'chunks_sent': 0, 'total_chunks': 0, 'throughput': 0.0,
        'start_time': None, 'recovery_time': None, 'result': None,
        'completed_at': None,
    })
    mod.event_log.clear()
    mod._events_ingested = 0
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def test_second_click_while_pending_is_rejected(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    second = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert second.status_code == 409
    assert 'already running' in second.get_json()['error'].lower()
    # The first request must still be intact for the bridge to pick up.
    with open(dash.REQUEST_FILE) as f:
        assert json.load(f)['filename'] == 'a.bin'


def test_second_click_while_running_is_rejected(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    os.unlink(dash.REQUEST_FILE)  # bridge picked it up
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 1,
                   'total': 10, 'throughput': 5.0}, f)
    second = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert second.status_code == 409


def test_new_transfer_allowed_after_terminal_progress(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    os.unlink(dash.REQUEST_FILE)
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': False}, f)
    with open(dash.RESULT_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'status': 'done'}, f)
    retry = client.post('/api/start-transfer', json={'filename': 'a.bin'})
    assert retry.status_code == 200


def test_metrics_reflect_bridge_progress(dash):
    client = dash.app.test_client()
    assert client.post('/api/start-transfer', json={'filename': 'a.bin'}).status_code == 200
    with open(dash.PROGRESS_FILE, 'w') as f:
        json.dump({'filename': 'a.bin', 'running': True, 'chunk': 3,
                   'total': 10, 'throughput': 42.0}, f)
    metrics = client.get('/api/metrics').get_json()
    assert metrics['throughput'] == 42.0
