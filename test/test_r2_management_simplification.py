"""Synthetic regressions; run only in test/run_isolated.py's source copy.

Reuse the real ConfigStore/storage onboarding fixture for rejection and locking
checks. Cloudflare control/object transports remain in-memory fakes throughout.
"""
from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

from services import config as config_module
from services import image_service, r2_management_service as management
from services.cloudflare_api import CloudflareAPIError
from test.test_r2_image_storage import image_bytes
from test.test_r2_integration import ObjectClient, integrated, onboard, save_settings
from test.test_r2_management import ACCOUNT, HOSTS, NOW, FakeCloudflare, FakeConfig, FakeStorage


@pytest.fixture
def mock_manager(monkeypatch):
    config = FakeConfig()
    storage = FakeStorage(config)
    cloud = FakeCloudflare(storage)
    clock = [NOW]
    api = mock.Mock(return_value=cloud)
    logs = mock.Mock()
    legacy = mock.Mock(return_value={"image_count": 0, "total_bytes": 0})
    monkeypatch.setattr(management, "config", config)
    monkeypatch.setattr(management, "image_storage_service", storage)
    monkeypatch.setattr(management, "CloudflareAPI", api)
    monkeypatch.setattr(management, "log_service", logs)
    monkeypatch.setattr(management, "_now", lambda: clock[0])
    monkeypatch.setattr(image_service, "legacy_storage_stats", legacy)
    return SimpleNamespace(config=config, storage=storage, cloud=cloud, clock=clock,
                           api=api, logs=logs, legacy=legacy, manager=management.R2ManagementService())


def test_overview_uses_one_account_summaries_call(mock_manager, monkeypatch):
    env = mock_manager
    summaries = [{"id": ACCOUNT, "status": "eligible", "usage": {"image_count": 0}, "domains": []}]
    read = mock.Mock(return_value=summaries)
    monkeypatch.setattr(env.storage, "account_summaries", read)
    for name in ("account_usage", "account_status"):
        monkeypatch.setattr(env.storage, name, mock.Mock(side_effect=AssertionError("per-account reread")))

    result = env.manager.overview()

    read.assert_called_once_with()
    env.legacy.assert_called_once_with()
    assert result == {"settings": env.config.get_public_image_storage_settings(),
                      "accounts": summaries, "legacy": env.legacy.return_value}


@pytest.mark.parametrize("entry", ["storage", "general"])
@pytest.mark.parametrize("settings", [
    pytest.param({"r2_accounts": [{"id": ACCOUNT, "bucket": "replacement-images"}]}, id="occupied-bucket"),
    pytest.param({"r2_accounts": [{"id": ACCOUNT, "account_id": "c" * 32}]}, id="occupied-account"),
    pytest.param({"r2_accounts": [{"id": ACCOUNT, "rate_limit_requests": 0}]}, id="invalid-rate"),
    pytest.param({"r2_accounts": [{"id": ACCOUNT, "domains": [{
        "id": HOSTS[0], "domain": HOSTS[0], "zone_id": "invalid-zone", "enabled": True,
    }]}]}, id="invalid-domain"),
    pytest.param({"cors_origins": ["https://app.example.test/not-an-origin"]}, id="invalid-cors"),
])
def test_rejected_settings_preserve_real_usage_protection_and_status(integrated, entry, settings):
    env = integrated
    onboard(env)
    stored = env.storage.save(image_bytes())
    before_config = env.store.path.read_bytes()
    before_index = env.storage.index_file.read_bytes()
    before_usage = env.storage.get_r2_usage(ACCOUNT)
    before_calls = copy.deepcopy((env.cloud.calls, env.cloud.usage_calls, env.remote.calls))
    assert env.storage.account_status(ACCOUNT) == "eligible"

    save_settings(env, settings, entry, status=400)

    assert env.store.path.read_bytes() == before_config
    assert env.storage.index_file.read_bytes() == before_index
    assert env.storage.get_r2_usage(ACCOUNT) == before_usage
    assert env.manager.overview()["accounts"][0]["status"] == "eligible"
    assert env.storage.public_url(stored.rel) == stored.url
    assert (env.cloud.calls, env.cloud.usage_calls, env.remote.calls) == before_calls


def test_settings_commit_and_invalidation_share_the_reservation_lock(integrated, monkeypatch):
    env = integrated
    onboard(env)
    before = env.store.get_image_storage_settings()
    events = []
    commit = env.store.update
    update_usage = env.storage.update_r2_usage
    assert env.storage._index_lock is config_module.IMAGE_STORAGE_LOCK

    def checked_commit(payload):
        assert config_module.IMAGE_STORAGE_LOCK._is_owned(), "commit must enter with the reservation lock held"
        result = commit(payload)
        events.append("commit")
        return result

    def checked_invalidation(account_id, updates):
        assert config_module.IMAGE_STORAGE_LOCK._is_owned(), "invalidation must remain inside the same lock"
        assert events[0] == "commit"
        assert env.storage.account(account_id)["bucket"] == "replacement-images"
        events.append("usage" if "complete" in updates else "protection")
        return update_usage(account_id, updates)

    monkeypatch.setattr(env.store, "update", checked_commit)
    monkeypatch.setattr(env.storage, "update_r2_usage", checked_invalidation)
    result = env.manager.save_settings({"r2_accounts": [{
        "id": ACCOUNT, "bucket": "replacement-images",
        "access_key_id": "", "secret_access_key": "", "api_token": "",
    }]})

    assert events == ["commit", "usage", "protection"]
    assert result["accounts"][0]["status"] == "domains_unprotected"
    usage = env.storage.get_r2_usage(ACCOUNT)
    assert usage["complete"] is usage["domains_safe"] is usage["domains_checked"] is False
    assert usage["verified_domains"] == []
    after = env.store.get_image_storage_settings()
    assert after["mode"] == before["mode"] == "r2"
    assert all(after["r2_accounts"][0][field] == before["r2_accounts"][0][field]
               for field in ("access_key_id", "secret_access_key", "api_token")), "blank credentials must retain existing values"


def test_pure_switch_does_not_make_a_failing_second_domain_read(mock_manager, monkeypatch):
    env = mock_manager
    domains = mock.Mock(side_effect=[
        copy.deepcopy(list(env.cloud.domains.values())),
        CloudflareAPIError("synthetic second read failure", status_code=503),
    ])
    managed = mock.Mock(wraps=env.cloud.get_managed_domain)
    monkeypatch.setattr(env.cloud, "list_domains", domains)
    monkeypatch.setattr(env.cloud, "get_managed_domain", managed)

    result = env.manager.update_domain(ACCOUNT, HOSTS[1], {"active": True}, "synthetic-admin")

    domains.assert_called_once_with("synthetic-images")
    managed.assert_called_once_with("synthetic-images")
    assert result["id"] == HOSTS[1] and result["enabled"] and result["protected"]
    assert env.config.settings["r2_accounts"][0]["active_domain_id"] == HOSTS[1]
    assert env.config.writes == 1 and env.cloud.calls == []
    assert env.storage.state["domains_checked"] is env.storage.state["domains_safe"] is True
    env.logs.add.assert_called_once()
    detail = env.logs.add.call_args.kwargs["detail"]
    assert (detail["old_domain"], detail["new_domain"], detail["actor"]) == (*HOSTS, "synthetic-admin")


def test_cloud_enabled_mutation_still_requires_write_confirmation(mock_manager, monkeypatch):
    env = mock_manager
    domains = mock.Mock(wraps=env.cloud.list_domains)
    write = mock.Mock(return_value={})  # CF acknowledges the call but leaves the binding unchanged.
    monkeypatch.setattr(env.cloud, "list_domains", domains)
    monkeypatch.setattr(env.cloud, "update_domain", write)

    with pytest.raises(management.R2ManagementError, match="CF 未确认"):
        env.manager.update_domain(ACCOUNT, HOSTS[1], {"enabled": False})

    write.assert_called_once_with("synthetic-images", HOSTS[1], False)
    assert domains.call_count == 2
    assert env.config.writes == 0
    assert env.config.settings["r2_accounts"][0]["domains"][1]["enabled"] is True
    assert env.storage.state["domains_checked"] is env.storage.state["domains_safe"] is False
    env.logs.add.assert_not_called()


def test_confirmed_shutdown_skips_ticks_and_fresh_queries_but_keeps_monitoring(mock_manager, monkeypatch):
    env = mock_manager
    env.cloud.usage_b = 80
    env.manager.refresh_usage(ACCOUNT)
    assert env.storage.state["shutdown_ok"] is True
    calls = copy.deepcopy(env.cloud.calls)
    account = env.config.settings["r2_accounts"][0]
    account["enabled"] = False
    for domain in account["domains"]:
        domain["enabled"] = False
    domains = mock.Mock(wraps=env.cloud.list_domains)
    managed = mock.Mock(wraps=env.cloud.get_managed_domain)
    monkeypatch.setattr(env.cloud, "list_domains", domains)
    monkeypatch.setattr(env.cloud, "get_managed_domain", managed)
    env.api.reset_mock()

    for _ in range(2):
        env.clock[0] += timedelta(minutes=1)
        assert env.manager.maintenance_tick()["accounts"] == [{"id": ACCOUNT, "ok": True}]
    env.api.assert_not_called()
    assert len(env.cloud.usage_calls) == 1

    env.clock[0] += timedelta(seconds=env.config.settings["usage_refresh_seconds"])
    assert env.manager.maintenance_tick()["accounts"] == [{"id": ACCOUNT, "ok": True}]
    assert len(env.cloud.usage_calls) == 2
    env.cloud.usage_b = 1
    env.manager.refresh_usage(ACCOUNT)
    assert len(env.cloud.usage_calls) == 3
    assert env.api.call_count == 2
    domains.assert_not_called()
    managed.assert_not_called()
    assert env.cloud.calls == calls
    assert env.storage.state["access_paused"] is env.storage.state["shutdown_ok"] is True
    assert env.storage.cleanup_count == 3


def test_new_budget_triggers_first_pause_even_with_an_old_shutdown_verdict(mock_manager):
    env = mock_manager
    env.storage.state.update(cloud_b=79, shutdown_ok=True)
    env.config.settings["r2_accounts"][0]["b_budget"] = 75

    result = env.manager.maintenance_tick()

    assert result["accounts"] == [{"id": ACCOUNT, "ok": True}]
    assert env.cloud.usage_calls == []  # Existing fresh counters enforce the new budget immediately.
    assert env.cloud.calls.count(("managed", False)) == 1
    assert env.storage.state["access_paused"] is env.storage.state["shutdown_ok"] is True
    assert all(env.cloud.gates_at_disable)
    assert not any(domain["enabled"] for domain in env.cloud.domains.values())


def test_expiry_cleanup_transport_runs_outside_management_lock(integrated, monkeypatch):
    env = integrated
    onboard(env)
    stored = env.storage.save(image_bytes())
    env.clock[0] += timedelta(minutes=2)
    delete = ObjectClient.delete_object
    deleted = []

    def checked_delete(client, key):
        assert not env.manager._lock._is_owned(), "expiry deletion must not hold the management lock"
        assert not config_module.IMAGE_STORAGE_LOCK._is_owned(), "object transport must not hold the storage lock"
        deleted.append(key)
        return delete(client, key)

    monkeypatch.setattr(ObjectClient, "delete_object", checked_delete)
    result = env.manager.maintenance_tick()

    assert result["accounts"] == [{"id": ACCOUNT, "ok": True}]
    assert result["cleanup"] == {"removed": 1, "failed": 0, "pending": 0}
    assert deleted == [stored.image_id]
    assert env.storage.metadata(stored.rel) is None
