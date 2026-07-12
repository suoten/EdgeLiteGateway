"""Debug _get_paths in pytest context."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from edgelite.app import create_app
from edgelite.middleware.request_id import RequestIdFilter


def _make_test_config(debug_api_enabled=False, cors_allowed_origins=None, allowed_hosts=None):
    server = SimpleNamespace(
        debug_api_enabled=debug_api_enabled,
        cors_allowed_origins=cors_allowed_origins or [],
        cors_origins=["http://localhost:3000"],
        allowed_hosts=allowed_hosts or [],
    )
    security = SimpleNamespace(
        secret_key="test-secret-key-for-app-testing-32+chars!!",
        secret_key_previous=None, algorithm="HS256", key_id="test-kid",
        previous_key_id="old-kid", max_token_ttl_days=30,
        access_token_expire_minutes=30, refresh_token_expire_days=7,
        csrf_secret="csrf-secret-key-for-app-testing-32+chars!",
        cookie_secure=False, rate_limit_requests_per_minute=120,
    )
    backup = SimpleNamespace(backup_dir="data/backups", interval_hours=24, retain_days=7, enabled=False)
    return SimpleNamespace(
        server=server, security=security, backup=backup,
        influxdb=SimpleNamespace(token="t", url="http://localhost:8086", org="e", bucket="e"),
        database=SimpleNamespace(backend="sqlite", sqlite_path="data/test.db"),
    )


@pytest.fixture(autouse=True)
def _clean_request_id_filter():
    yield
    import logging
    root_logger = logging.getLogger()
    for f in list(root_logger.filters):
        if isinstance(f, RequestIdFilter):
            root_logger.removeFilter(f)


@pytest.fixture
def test_config():
    return _make_test_config()


@pytest.fixture
def patched_config(test_config, monkeypatch):
    monkeypatch.setattr("edgelite.app.get_config", lambda: test_config)
    monkeypatch.setattr("edgelite.config.get_config", lambda: test_config)
    monkeypatch.setattr("edgelite.security.jwt.get_config", lambda: test_config)
    return test_config


@pytest.fixture
def mock_lifespan_deps(monkeypatch):
    mock_bootstrap = AsyncMock()
    mock_teardown = AsyncMock()
    monkeypatch.setattr("edgelite.app.bootstrap_all", mock_bootstrap)
    monkeypatch.setattr("edgelite.app.teardown", mock_teardown)
    mock_rate_repo = MagicMock()
    mock_rate_repo.start_cleanup_task = MagicMock()
    mock_rate_repo.stop_cleanup_task = AsyncMock()
    monkeypatch.setattr("edgelite.storage.sqlite_repo.RateLimitRepo", mock_rate_repo)
    mock_backup_svc = AsyncMock()
    mock_backup_svc.start_scheduler = AsyncMock()
    mock_backup_svc.stop_scheduler = AsyncMock()
    monkeypatch.setattr("edgelite.services.system_services.get_backup_service", MagicMock(return_value=mock_backup_svc))
    mock_db_scheduler = AsyncMock()
    mock_db_scheduler.start = AsyncMock()
    mock_db_scheduler.stop = AsyncMock()
    monkeypatch.setattr("edgelite.services.backup_scheduler.get_backup_scheduler", MagicMock(return_value=mock_db_scheduler))


@pytest.fixture
def app(patched_config, mock_lifespan_deps, monkeypatch):
    monkeypatch.setattr("edgelite.app._mount_frontend", lambda app: None)
    monkeypatch.delenv("DEV_MODE", raising=False)
    return create_app()


class TestDebug:
    def test_debug_paths(self, app):
        """Debug route path collection."""
        # Method 1: Simple path check
        paths1 = set()
        for r in app.routes:
            path = getattr(r, "path", None)
            if path:
                paths1.add(path)
        print(f"\nMethod 1 (simple path): {len(paths1)} paths")
        print(f"  Sample: {sorted(paths1)[:5]}")

        # Method 2: With original_router
        paths2 = set()
        for r in app.routes:
            path = getattr(r, "path", None)
            if path:
                paths2.add(path)
            original = getattr(r, "original_router", None)
            if original is not None:
                print(f"  Found _IncludedRouter, original type: {type(original).__name__}")
                orig_routes = getattr(original, "routes", [])
                print(f"  original.routes count: {len(orig_routes)}")
                for ir in orig_routes:
                    ip = getattr(ir, "path", None)
                    if ip:
                        paths2.add(ip)
        print(f"\nMethod 2 (with original_router): {len(paths2)} paths")
        print(f"  Sample: {sorted(paths2)[:5]}")
        print(f"  Has /api/v1/auth/login: {'/api/v1/auth/login' in paths2}")
        print(f"  Has /health/live: {'/health/live' in paths2}")

        # Method 3: Check route types
        from collections import Counter
        type_counts = Counter(type(r).__name__ for r in app.routes)
        print(f"\nRoute types: {dict(type_counts)}")

        # Method 4: Check first _IncludedRouter attributes
        for r in app.routes:
            if type(r).__name__ == '_IncludedRouter':
                print(f"\n_IncludedRouter attrs: {[a for a in dir(r) if not a.startswith('_')]}")
                orig = getattr(r, 'original_router', 'MISSING')
                print(f"  original_router: {type(orig).__name__ if orig != 'MISSING' else 'MISSING'}")
                if orig != 'MISSING':
                    print(f"  original.routes: {len(orig.routes)}")
                break

        assert True  # Just for debugging
