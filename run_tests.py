#!/usr/bin/env python3
"""Run all CN-Mini tests."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Run each test module
test_modules = [
    'tests.test_transfer',
    'tests.test_recovery',
    'tests.test_failure',
    'tests.test_edge_cases',
    'tests.test_bridge_request',
    'tests.test_bridge_retry',
    'tests.test_dashboard_busy',
    'tests.test_dashboard_server',
    'tests.test_e2e_local',
    'tests.test_serverctl',
    'tests.test_controller_reroute',
]

total_passed = 0
total_failed = 0
total_errors = 0

for mod_name in test_modules:
    print(f"\n{'='*60}")
    print(f"Running {mod_name}")
    print(f"{'='*60}")
    try:
        mod = __import__(mod_name, fromlist=['*'])
        # Find all test classes and functions
        import unittest
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(mod)
        runner = unittest.TextTestRunner(verbosity=2)
        result = runner.run(suite)
        total_passed += result.testsRun - len(result.failures) - len(result.errors)
        total_failed += len(result.failures)
        total_errors += len(result.errors)
    except Exception as e:
        print(f"  ERROR loading {mod_name}: {e}")
        total_errors += 1

print(f"\n{'='*60}")
print(f"SUMMARY: {total_passed} passed, {total_failed} failed, {total_errors} errors")
print(f"{'='*60}")

sys.exit(0 if total_failed == 0 and total_errors == 0 else 1)
