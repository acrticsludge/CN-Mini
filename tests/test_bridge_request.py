"""
Bridge request-race regressions — no Mininet needed.

Pins the "took 5 clicks" bug: clear_transfer_files() used to delete the
pending REQUEST_FILE too, so a dashboard click landing between the consume and
the clear lost its request and the transfer never started.
"""

import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_bridge():
    spec = importlib.util.spec_from_file_location('bridge_under_test', SPEC_PATH)
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
    return mod


def test_clear_preserves_pending_request(tmp_path):
    mod = _point_at(_load_bridge(), tmp_path)
    for p in (mod.REQUEST_FILE, mod.RESULT_FILE, mod.PROGRESS_FILE,
              mod.EVENTS_FILE, mod.CANCEL_FILE):
        with open(p, 'w') as f:
            f.write('{}')
    mod.clear_transfer_files()
    assert os.path.exists(mod.REQUEST_FILE), 'pending request was deleted'
    for p in (mod.RESULT_FILE, mod.PROGRESS_FILE, mod.EVENTS_FILE, mod.CANCEL_FILE):
        assert not os.path.exists(p)


def test_process_request_rejects_bad_filename(tmp_path):
    mod = _point_at(_load_bridge(), tmp_path)
    res = mod.process_request({'filename': '../evil'})
    assert res['status'] == 'failed'


def test_process_request_missing_file(tmp_path):
    mod = _point_at(_load_bridge(), tmp_path)
    res = mod.process_request({'filename': 'nope.bin'})
    assert res['status'] == 'failed'
    assert 'not found' in res['error'].lower()


def test_terminal_progress_unmarks_busy(tmp_path):
    mod = _point_at(_load_bridge(), tmp_path)
    mod._write_terminal_progress('a.bin', running=False)
    with open(mod.PROGRESS_FILE) as f:
        assert json.load(f)['running'] is False
