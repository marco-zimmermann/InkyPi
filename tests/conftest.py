"""Unit workers never inspect the developer/CI host's real memory cgroup."""
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def mock_worker_resource_probe(monkeypatch):
    monkeypatch.setattr('refresh_task.CgroupResourceMonitor',
                        lambda: Mock(check=Mock(return_value=None)))
