"""
Demonstrates ReuseServerTestCase usage.

All tests in this class share ONE server. Between each test, FLUSHALL + CONFIG
RESETSTAT run automatically to give each test a clean slate without the cost of
restarting the server.

Tests run top-to-bottom in definition order (via pytest-order with
--order-scope=class). Some tests verify isolation from the previous test,
so ordering matters.
"""

import pytest
from conftest import resource_port_tracker
from valkey_test_case import ReuseServerTestCase


class TestReuseServer(ReuseServerTestCase):
    """Verifies that server reuse works and tests are isolated."""

    @pytest.fixture(autouse=True)
    def setup_test(self, setup):
        self.server, self.client = self.create_server(testdir=self.testdir)

    def test_write_and_read(self):
        """Basic write/read on the shared server."""
        self.client.set("greeting", "hello")
        assert self.client.get("greeting") == b"hello"

    def test_isolation_from_previous(self):
        """Proves FLUSHALL cleaned up the previous test's data."""
        result = self.client.get("greeting")
        assert result is None, "Key from previous test should not exist"

    def test_server_still_alive(self):
        """Proves the server survived across tests (no restart)."""
        assert self.client.ping() is True

    def test_multiple_keys(self):
        """Write multiple keys, verify they all exist within this test."""
        for i in range(10):
            self.client.set(f"key:{i}", f"value:{i}")
        assert self.client.dbsize() == 10

    def test_previous_keys_gone(self):
        """Proves the 10 keys from the previous test were flushed."""
        assert self.client.dbsize() == 0

    def test_config_change_is_restored(self):
        """Proves configs modified during a test get restored for the next."""
        original = self.client.config_get("hz")["hz"]
        self.client.config_set("hz", "50")
        assert self.client.config_get("hz")["hz"] == "50"

    def test_config_restored_after_previous(self):
        """Proves the config changed in the previous test was reset."""
        current = self.client.config_get("hz")["hz"]
        assert current == "10", f"Expected hz=10 (default), got hz={current}"
