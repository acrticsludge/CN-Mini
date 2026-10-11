"""
Static-analysis gate for the whole project.

ast.parse only catches syntax errors, so a file can parse cleanly and still
crash the moment it runs on an undefined name — which is exactly how the
`NameError: name 'sys' is not defined` bug reached the VM. pyflakes does scope
analysis and catches those, so it runs here as a gate.
"""

import ast
import os

import pytest

PROJECT_FILES = [
    'common/protocol.py',
    'common/checksum.py',
    'common/transport.py',
    'server/server.py',
    'client/client.py',
    'mininet/topology.py',
    'mininet/bridge.py',
    'mininet/linkctl.py',
    'controller/sdn_controller.py',
    'frontend/app.py',
    'tests/test_transfer.py',
    'tests/test_recovery.py',
    'tests/test_failure.py',
    'tests/test_controller_reroute.py',
    'tests/test_bridge_request.py',
    'tests/test_bridge_singleton.py',
    'tests/test_dashboard_busy.py',
    'tests/test_e2e_local.py',
    'tests/test_lint.py',
]


def project_root():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


@pytest.mark.parametrize('rel', PROJECT_FILES)
def test_file_parses(rel):
    """Every source file must be syntactically valid Python."""
    path = os.path.join(project_root(), rel)
    assert os.path.exists(path), f'missing source file: {rel}'
    with open(path, encoding='utf-8') as f:
        ast.parse(f.read(), filename=rel)


def test_no_undefined_names():
    """No file may reference a name that is never imported or assigned.

    pyflakes is required; skip the test (never pass it silently) when absent.
    """
    pyflakes_checker = pytest.importorskip('pyflakes.checker')

    problems = []
    for rel in PROJECT_FILES:
        path = os.path.join(project_root(), rel)
        if not os.path.exists(path):
            problems.append(f'{rel}: missing')
            continue
        with open(path, encoding='utf-8') as f:
            source = f.read()
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as e:
            problems.append(f'{rel}: {e}')
            continue
        checker = pyflakes_checker.Checker(tree, filename=rel)
        for message in getattr(checker, 'messages', []):
            text = str(message)
            # Only the categories that break at runtime. Style noise such as
            # unused imports is the linter's business, not a test failure.
            if 'undefined name' in text or 'syntax' in text.lower():
                problems.append(f'{rel}: {text}')

    assert not problems, 'Runtime-breaking static problems:\n' + '\n'.join(problems)


def test_no_star_imports():
    """Star imports hide undefined names from the gate above.

    Found by this very test: `from common.protocol import *` made the test
    files invisible to static analysis.
    """
    offenders = []
    for rel in PROJECT_FILES:
        path = os.path.join(project_root(), rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == '*':
                        offenders.append(f'{rel}: from {node.module} import *')
    assert not offenders, 'Replace star imports with explicit names:\n' + '\n'.join(offenders)
