"""
Link control for the Mininet topology.

Kept separate from topology.py so the logic can be imported (and tested)
without Mininet installed: everything here operates on a net-like object
exposing .links, .nameToNode and optionally .configLinkState().
"""

import time


def apply_link_state(net, sw1, sw2, state):
    """Bring the link between two switches up or down.

    Uses the net's own configLinkState when available — it emits the
    OFPT_PORT_STATUS event the Ryu controller listens for — and otherwise falls
    back to taking the interfaces down directly.

    Returns (ok, message).
    """
    if state not in ('up', 'down'):
        return False, f'unknown state {state!r}'
    if sw1 == sw2:
        return False, 'Pick two different switches'

    for name in (sw1, sw2):
        if name not in getattr(net, 'nameToNode', {}):
            return False, f'unknown switch {name!r}'

    if hasattr(net, 'configLinkState'):
        try:
            net.configLinkState(sw1, sw2, state)
            return True, f'{sw1}<->{sw2} {state}'
        except Exception:
            pass  # fall through to the manual path

    nodes = {sw1, sw2}
    matched = False
    for link in net.links:
        ends = {link.intf1.node.name, link.intf2.node.name}
        if ends != nodes:
            continue
        matched = True
        for intf in (link.intf1, link.intf2):
            intf.ifconfig(state)

    if not matched:
        return False, f'no link between {sw1} and {sw2}'
    return True, f'{sw1}<->{sw2} {state}'


def read_json(path):
    """Read a JSON file, returning None if missing or unparseable."""
    import os
    import json
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path, data):
    """Write JSON atomically so a reader never sees a half-written file."""
    import os
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        import json
        json.dump(data, f)
    os.replace(tmp, path)


def serve_link_requests(net, request_file, result_file, poll=0.3,
                        should_continue=None):
    """Poll for dashboard link requests and apply them.

    Runs in the main thread so the interactive CLI can occupy another one.
    Returns when should_continue() is false, if given.
    """
    print(f'[LINKCTL] Link request watcher ready ({request_file})', flush=True)
    import os
    import json
    while True:
        try:
            if os.path.isfile(request_file):
                with open(request_file, 'r') as f:
                    req = json.load(f)
                os.unlink(request_file)

                sw1, sw2 = req.get('sw1'), req.get('sw2')
                state = req.get('state')
                print(f'[LINKCTL] Link request: {sw1} <-> {sw2} -> {state}', flush=True)

                ok, message = apply_link_state(net, sw1, sw2, state)
                result = {
                    'sw1': sw1, 'sw2': sw2, 'state': state,
                    # Echo the request id so the dashboard can tell a fresh
                    # confirmation from a stale file left by an earlier click.
                    'request_id': req.get('request_id'),
                    'applied': ok, 'message': message,
                    'at': time.strftime('%H:%M:%S'),
                }
                if not ok:
                    result['error'] = message
                    print(f'[LINKCTL] FAILED: {message}', flush=True)
                else:
                    print(f'[LINKCTL] Applied: {message}', flush=True)

                write_json_atomic(result_file, result)
        except Exception as e:
            print(f'[LINKCTL] watcher error: {e}', flush=True)

        if should_continue is not None and not should_continue():
            break
        time.sleep(poll)
