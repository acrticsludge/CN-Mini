"""
Bridge singleton-lock regressions — no Mininet needed.

Every topology.py run spawns a bridge and nothing reaps the old one. Without
the lock, two bridges watch the same request file: duplicate transfers and
interleaved progress/result writes, surfacing as "stored but UI says failed".
"""

import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

SPEC_PATH = os.path.join(os.path.dirname(__file__), '..', 'mininet', 'bridge.py')


def _load_bridge(name='bridge_lock_under_test'):
    spec = importlib.util.spec_from_file_location(name, SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_second_live_bridge_is_refused(tmp_path):
    mod = _load_bridge()
    mod.LOCK_FILE = str(tmp_path / 'bridge.lock')
    assert mod.claim_bridge_lock() is True
    # Touched just now: a second bridge must back off, not race.
    assert mod.claim_bridge_lock() is False


def test_stale_lock_is_reclaimed(tmp_path):
    mod = _load_bridge()
    lock = tmp_path / 'bridge.lock'
    lock.write_text('dead-pid')
    old = time.time() - 120
    os.utime(str(lock), (old, old))
    mod.LOCK_FILE = str(lock)
    assert mod.claim_bridge_lock() is True


def test_heartbeat_refreshes_alive_and_lock(tmp_path):
    """Heartbeats must be callable mid-run (no args) and keep both files fresh,
    or the dashboard declares the bridge dead during long transfers."""
    mod = _load_bridge()
    mod.ALIVE_FILE = str(tmp_path / 'bridge_alive.json')
    mod.LOCK_FILE = str(tmp_path / 'bridge.lock')
    import pathlib
    pathlib.Path(mod.LOCK_FILE).write_text('1')
    old = time.time() - 120
    os.utime(mod.LOCK_FILE, (old, old))
    mod.write_heartbeat()
    assert os.path.getmtime(mod.LOCK_FILE) > old + 100
    import json
    with open(mod.ALIVE_FILE) as f:
        alive = json.load(f)
    assert alive.get('pid') == os.getpid()
