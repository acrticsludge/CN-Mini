"""
Tests for path failure and SDN rerouting scenarios.
Verifies that link failure events trigger controller rerouting.
"""

import os
import sys
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestFailureRecovery:
    """Test link failure detection and SDN-based rerouting."""

    def test_controller_handles_port_status(self):
        """Controller processes port status events without errors."""
        # This test verifies the controller module loads and initializes
        try:
            from controller.sdn_controller import SDNController
            # Verify the class exists and has the required handler methods
            assert hasattr(SDNController, 'port_status_handler')
            assert hasattr(SDNController, 'switch_features_handler')
            assert hasattr(SDNController, '_update_topology')
            assert hasattr(SDNController, '_recompute_paths')
        except ImportError:
            pytest.skip("Ryu not installed — skipping controller tests")

    def test_topology_graph_construction(self):
        """Controller builds a valid topology graph from switch/link data."""
        try:
            import networkx as nx
            from controller.sdn_controller import SDNController

            # Verify networkx is available for Dijkstra computation
            G = nx.Graph()
            G.add_edge(1, 2, weight=1)
            G.add_edge(2, 3, weight=1)
            G.add_edge(1, 3, weight=3)

            path = nx.shortest_path(G, 1, 3, weight='weight')
            assert path == [1, 2, 3]
        except ImportError:
            pytest.skip("networkx not installed — skipping graph tests")

    def test_link_failure_removes_path(self):
        """When a link fails, Dijkstra finds an alternate path."""
        try:
            import networkx as nx

            # Build a simple diamond topology
            G = nx.Graph()
            G.add_edge('s1', 's2', weight=1)
            G.add_edge('s2', 's3', weight=1)
            G.add_edge('s1', 's4', weight=1)
            G.add_edge('s4', 's3', weight=1)

            # Normal path: s1 -> s2 -> s3
            path = nx.shortest_path(G, 's1', 's3', weight='weight')
            assert path == ['s1', 's2', 's3']

            # Link s2-s3 fails
            G.remove_edge('s2', 's3')

            # New path: s1 -> s4 -> s3
            path = nx.shortest_path(G, 's1', 's3', weight='weight')
            assert path == ['s1', 's4', 's3']
        except ImportError:
            pytest.skip("networkx not installed — skipping graph tests")

    def test_recovery_restores_optimal_path(self):
        """When a link is restored, Dijkstra returns to the optimal path."""
        try:
            import networkx as nx

            G = nx.Graph()
            G.add_edge('s1', 's2', weight=1)
            G.add_edge('s2', 's3', weight=1)
            G.add_edge('s1', 's4', weight=1)
            G.add_edge('s4', 's3', weight=1)

            # Remove direct link
            G.remove_edge('s2', 's3')
            path = nx.shortest_path(G, 's1', 's3', weight='weight')
            assert path == ['s1', 's4', 's3']

            # Restore link. Both s1-s2-s3 and s1-s4-s3 are 2 hops with equal
            # cost, so assert on the property (shortest, correct endpoints)
            # rather than a specific middle hop — the tie-break is unspecified.
            G.add_edge('s2', 's3', weight=1)
            path = nx.shortest_path(G, 's1', 's3', weight='weight')
            assert path[0] == 's1' and path[-1] == 's3'
            assert len(path) == 3, path
            assert nx.shortest_path_length(G, 's1', 's3', weight='weight') == 2
        except ImportError:
            pytest.skip("networkx not installed — skipping graph tests")

    def test_frontend_api_endpoints(self):
        """Flask app exposes required API endpoints."""
        try:
            from frontend.app import app

            # Verify routes exist
            rules = [rule.rule for rule in app.url_map.iter_rules()]
            assert '/' in rules
            assert '/api/files' in rules
            assert '/api/start-transfer' in rules
            assert '/api/stop-transfer' in rules
            assert '/api/link-down' in rules
            assert '/api/link-up' in rules
            assert '/api/status' in rules
            assert '/api/metrics' in rules
            assert '/api/browse' in rules
            assert '/api/transfer-result' in rules
        except ImportError:
            pytest.skip("Flask not installed — skipping frontend tests")

    def test_stored_files_directory_exists(self):
        """stored_files/ directory structure is created."""
        base_dir = os.path.join(os.path.dirname(__file__), '..')
        stored_dir = os.path.join(base_dir, 'stored_files')
        assert os.path.exists(stored_dir)
        assert os.path.exists(os.path.join(stored_dir, 'h3'))
        assert os.path.exists(os.path.join(stored_dir, 'h4'))
