"""
Bridge retry and failover regressions — no Mininet needed.

The bridge owns the decision after a client failure: retry the same server
with the client's resume point, or restart on the backup server. These tests
pin the two paths and the cancel window.
"""

import importlib.util
import json
import os
import sys

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_bridge():
    spec = importlib.util.spec_from_file_location('bridge_retry_under_test', SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _point_at(mod, tmp_path):
    shared = str(tmp_path)
    mod.SHARED_DIR = shared
    mod.REQUEST_FILE = os.path.join(shared, 'transfer_request.json')
    mod.RESULT_FILE = os.path.join(shared, 'transfer_result.json')
    mod.PROGRESS_FILE = os.path.join(shared, 'transfer_progress.json')
    mod.EVENTS_FILE = os.path.join(shared, 'transfer_events.json')
    mod.CANCEL_FILE = os.path.join(shared, 'transfer_cancel.json')
    with open(os.path.join(shared, 'a.bin'), 'wb') as f:
        f.write(b'x' * 100)
    return mod


def _result(status='failed', resume=None, server='h3', addr='10.0.0.3:9000',
            chunk=2, total=4):
    return {'status': status, 'error': None if status == 'done' else 'lost',
            'resume_point': resume, 'server': server, 'server_addr': addr,
            'chunk': chunk, 'total': total, 'throughput': 0.0}


def test_retry_same_server_with_resume(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    results = [_result(resume=2), _result(status='done', chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'up')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['attempts'] == 2
    assert out['resumed_from'] == 2
    assert calls[1]['resume_from'] == 2
    assert calls[1]['servers'] == ['10.0.0.3:9000']


def test_failover_to_h4_starts_at_zero(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    results = [_result(resume=2),
               _result(status='done', server='h4', addr='10.0.0.4:9000',
                       chunk=4)]

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return results.pop(0)

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'down')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'done'
    assert out['restarted_on_backup'] is True
    assert out['attempts'] == 2
    assert calls[1]['resume_from'] == 0
    assert calls[1]['servers'] == ['10.0.0.4:9000']


def test_cancel_during_wait_stops(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    monkeypatch.setattr(mod, 'run_client',
                        lambda *a, **k: (calls.append(k), _result(resume=2))[1])
    monkeypatch.setattr(mod, '_wait_for_server', lambda addr, rid: 'cancelled')
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'stopped'
    assert len(calls) == 1


def test_failure_without_resume_point_does_not_retry(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    calls = []
    monkeypatch.setattr(mod, 'run_client',
                        lambda *a, **k: (calls.append(k), _result())[1])
    out = mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert out['status'] == 'failed'
    assert len(calls) == 1


def test_progress_holds_chunk_during_retry(tmp_path, monkeypatch):
    mod = _point_at(_load_bridge(), tmp_path)
    seen = {}
    results = [_result(resume=2), _result(status='done', chunk=4)]

    def fake_run(*args, **kwargs):
        return results.pop(0)

    def fake_wait(addr, rid):
        with open(mod.PROGRESS_FILE) as f:
            seen.update(json.load(f))
        return 'up'

    monkeypatch.setattr(mod, 'run_client', fake_run)
    monkeypatch.setattr(mod, '_wait_for_server', fake_wait)
    mod.process_request({'filename': 'a.bin', 'request_id': 'r1'})

    assert seen['phase'] == 'retrying'
    assert seen['chunk'] == 2
    assert seen['total'] == 4
    assert seen['running'] is True
