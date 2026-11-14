"""Synthetic, network-mocked tests. Run only through the isolated test runner."""
from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import services.r2_management_service as module
from services.cloudflare_api import CloudflareAPIError
from services.r2_client import R2Error


ACCOUNT = "00000000-0000-4000-8000-000000000001"
ZONE = "b" * 32
NOW = datetime(2026, 6, 12, 12, tzinfo=UTC)
HOSTS = ["images.example.test", "backup.example.test"]
PHASES = ["http_ratelimit", "http_request_firewall_custom", "http_request_cache_settings"]


def settings_fixture():
    return {
        "enabled": True, "mode": "r2", "cors_origins": ["*"], "usage_refresh_seconds": 300,
        "usage_max_age_seconds": 900, "cleanup_interval_seconds": 60,
        "r2_accounts": [{
            "id": ACCOUNT, "name": "synthetic", "enabled": True, "account_id": "a" * 32,
            "bucket": "synthetic-images", "access_key_id": "synthetic-key", "secret_access_key": "synthetic-secret",
            "api_token": "synthetic-token", "capacity_bytes": 8000, "a_budget": 800,
            "b_warn": 70, "b_budget": 80, "billing_cycle_day": 1, "billing_cycle_confirmed": True,
            "rate_limit_requests": 120, "restrict_methods": True, "active_domain_id": HOSTS[0],
            "domains": [{"id": host, "domain": host, "zone_id": ZONE, "enabled": True} for host in HOSTS],
        }],
    }


class FakeConfig:
    def __init__(self):
        self.settings = settings_fixture()
        self.writes = 0

    def get_image_storage_settings(self):
        return copy.deepcopy(self.settings)

    def get_public_image_storage_settings(self):
        settings = self.get_image_storage_settings()
        for account in settings["r2_accounts"]:
            for field in ("api_token", "access_key_id", "secret_access_key"):
                account["has_" + field] = bool(account[field])
                account[field] = ""
        return settings

    def update(self, payload):
        self.writes += 1
        self.settings.update(copy.deepcopy(payload["image_storage"]))


class FakeStorage:
    def __init__(self, config):
        self.config = config
        self.state = {"verified_domains": HOSTS[:], "managed_disabled": True, "domains_checked": True,
                      "domains_safe": True, "complete": True, "a_local": 15, "cloud_a": 10, "cloud_b": 1,
                      "fetched_at": "2026-06-12T11:59:00Z", "period_start": "2026-06-01T00:00:00Z"}
        self.updates = []
        self.snapshots = []
        self.items = []
        self.cleanup_count = 0

    def account(self, account_id):
        return copy.deepcopy(next(item for item in self.config.settings["r2_accounts"] if item["id"] == account_id))

    def get_r2_usage(self, account_id):
        return copy.deepcopy(self.state)

    def update_r2_usage(self, account_id, updates):
        self.updates.append(copy.deepcopy(updates))
        updates = copy.deepcopy(updates)
        increment = updates.pop("a_local_increment", 0)
        self.state.update(updates)
        if increment:
            self.state["a_local"] = max(self.state.get("a_local", 0), self.state.get("cloud_a", 0)) + increment
        return self.get_r2_usage(account_id)

    def record_usage_snapshot(self, account_id, result):
        self.snapshots.append(copy.deepcopy(result))
        self.state.update(cloud_a=result["a_operations"], cloud_b=result["b_operations"],
                          fetched_at=result["fetched_at"], period_start=result["period_start"],
                          complete=True, a_local=max(self.state["a_local"], result["a_operations"]))
        return self.get_r2_usage(account_id)

    def account_usage(self, account_id):
        return {**self.get_r2_usage(account_id), "local_bytes": 0, "pending_bytes": 0, "image_count": len(self.items)}

    def account_status(self, account_id):
        return "access_paused" if self.state.get("access_paused") else "eligible"

    def account_summaries(self):
        summaries = []
        for account in self.config.settings["r2_accounts"]:
            usage = self.account_usage(account["id"])
            summaries.append({"id": account["id"], "status": self.account_status(account["id"]),
                              "usage": usage, "domains": usage.get("domains", [])})
        return summaries

    def list_items(self, base_url):
        return copy.deepcopy(self.items)

    def cleanup_expired(self):
        self.cleanup_count += 1
        return {"removed": 0, "failed": 0, "pending": 0}


class FakeCloudflare:
    def __init__(self, storage):
        self.storage = storage
        self.domains = {host: {"domain": host, "zoneId": ZONE, "enabled": True,
                               "status": {"ownership": "active", "ssl": "active"}} for host in HOSTS}
        self.managed = False
        self.calls = []
        self.usage_calls = []
        self.usage_b = 1
        self.usage_complete = True
        self.usage_error = None
        self.disable_failures = set()
        self.enable_failures = set()
        self.rules_error = False
        self.gates_at_disable = []
        self.rulesets = {phase: {"id": phase + "-set", "kind": "zone", "phase": phase,
                                "rules": [{"id": phase + "-user", "ref": "user-owned", "enabled": True,
                                           "expression": 'http.request.uri.path eq "/private"', "action": "block"}]}
                         for phase in PHASES}

    def close(self):
        pass

    def list_zones(self):
        return [{"id": ZONE, "name": "example.test"}]

    def list_domains(self, bucket):
        return copy.deepcopy(list(self.domains.values()))

    def get_managed_domain(self, bucket):
        return {"enabled": self.managed}

    def set_managed_domain(self, bucket, enabled):
        self.calls.append(("managed", enabled))
        self.managed = enabled
        return {"enabled": enabled}

    def add_domain(self, bucket, domain, zone_id, enabled=False):
        self.calls.append(("add", domain, enabled))
        self.domains[domain] = {"domain": domain, "zoneId": zone_id, "enabled": enabled,
                                "status": {"ownership": "active", "ssl": "active"}}
        return copy.deepcopy(self.domains[domain])

    def update_domain(self, bucket, domain, enabled):
        self.calls.append(("domain", domain, enabled))
        if not enabled:
            self.gates_at_disable.append(bool(self.storage.state.get("access_paused")))
        if domain in (self.enable_failures if enabled else self.disable_failures):
            raise CloudflareAPIError("synthetic-private-transport", status_code=403, codes=(10000,))
        self.domains[domain]["enabled"] = enabled
        return copy.deepcopy(self.domains[domain])

    def delete_domain(self, bucket, domain):
        self.calls.append(("delete", domain))
        del self.domains[domain]

    def set_cors(self, bucket, origins):
        self.calls.append(("cors", copy.deepcopy(origins)))

    def query_usage(self, start, end, bucket):
        self.usage_calls.append((start, end, bucket))
        if self.usage_error:
            raise self.usage_error
        return {"a_operations": 20, "b_operations": self.usage_b, "bucket_a_operations": 1,
                "bucket_b_operations": 1, "storage_bytes": 100, "bucket_storage_bytes": 10,
                "object_count": 1, "complete": self.usage_complete, "period_start": start,
                "period_end": end, "unknown_actions": [] if self.usage_complete else ["unknown"]}

    def request(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, copy.deepcopy(json)))
        if self.rules_error and method != "GET":
            raise CloudflareAPIError("synthetic-private-quota", status_code=400, codes=(20000,))
        if path.endswith("/rulesets"):
            if method == "GET":
                return [{key: value for key, value in item.items() if key != "rules"} for item in self.rulesets.values()]
            entry = {**copy.deepcopy(json), "id": json["phase"] + "-set"}
            for rule in entry["rules"]:
                rule["id"] = rule["ref"] + "-id"
            self.rulesets[entry["phase"]] = entry
            return copy.deepcopy(entry)
        entry = next(item for item in self.rulesets.values() if "/" + item["id"] in path)
        if method == "GET":
            return copy.deepcopy(entry)
        if method == "POST":
            entry["rules"].append({**copy.deepcopy(json), "id": json["ref"] + "-id"})
        elif method == "PATCH":
            target = next(rule for rule in entry["rules"] if path.endswith("/" + rule["id"]))
            target.update(copy.deepcopy(json))
        else:
            raise AssertionError("whole-ruleset overwrite is forbidden")
        return copy.deepcopy(entry)


class R2ManagementTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        self.storage = FakeStorage(self.config)
        self.cloud = FakeCloudflare(self.storage)
        self.service = module.R2ManagementService()
        self.logs = mock.Mock()
        for patcher in (
            mock.patch.object(module, "config", self.config),
            mock.patch.object(module, "image_storage_service", self.storage),
            mock.patch.object(module, "CloudflareAPI", return_value=self.cloud),
            mock.patch.object(module, "log_service", self.logs),
            mock.patch.object(module, "_now", return_value=NOW),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_billing_period_is_utc_anchored_including_year_boundary(self):
        self.assertEqual(module.billing_period_start(datetime(2026, 1, 2, tzinfo=UTC), 28), datetime(2025, 12, 28, tzinfo=UTC))
        self.assertEqual(module.billing_period_start(datetime(2026, 2, 28, tzinfo=UTC), 28), datetime(2026, 2, 28, tzinfo=UTC))
        self.assertEqual(module.billing_period_start(NOW, 1), datetime(2026, 6, 1, tzinfo=UTC))

    def test_refresh_uses_full_cycle_snapshot_without_absolute_a_write(self):
        result = self.service.refresh_usage(ACCOUNT)
        self.assertEqual(self.cloud.usage_calls[0][:2], ("2026-06-01T00:00:00Z", "2026-06-12T12:00:00Z"))
        self.assertEqual(result["cloud_a"], 20)
        self.assertEqual(len(self.storage.snapshots), 1)
        self.assertTrue(all("a_local" not in update for update in self.storage.updates))

    def test_unconfirmed_cycle_never_queries_or_resets_counters(self):
        self.config.settings["r2_accounts"][0]["billing_cycle_confirmed"] = False
        with self.assertRaisesRegex(module.R2ManagementError, "计费周期"):
            self.service.refresh_usage(ACCOUNT)
        self.assertEqual(self.cloud.usage_calls, [])
        self.assertEqual(self.storage.state["a_local"], 15)
        self.assertEqual(self.storage.snapshots, [])

    def test_incomplete_usage_preserves_last_counts_and_timestamp(self):
        old = copy.deepcopy(self.storage.state)
        self.cloud.usage_complete = False
        with self.assertRaises(module.R2ManagementError):
            self.service.refresh_usage(ACCOUNT)
        self.assertEqual(self.storage.state["fetched_at"], old["fetched_at"])
        self.assertEqual(self.storage.state["cloud_a"], old["cloud_a"])
        self.assertEqual(self.storage.state["cloud_b"], old["cloud_b"])
        self.assertFalse(self.storage.state["complete"])
        self.assertEqual(self.storage.snapshots, [])

    def test_incomplete_high_b_still_shuts_down_once_without_accepting_snapshot(self):
        self.cloud.usage_complete = False
        self.cloud.usage_b = 80
        with self.assertRaises(module.R2ManagementError):
            self.service.refresh_usage(ACCOUNT)
        self.assertTrue(self.storage.state["access_paused"])
        self.assertTrue(self.storage.state["shutdown_ok"])
        self.assertEqual(self.cloud.calls.count(("managed", False)), 1)
        self.assertEqual(self.storage.snapshots, [])

    def test_usage_failure_does_not_expose_transport_and_still_closes_paused(self):
        self.storage.state["access_paused"] = True
        self.cloud.usage_error = CloudflareAPIError("synthetic-private-token", status_code=403, codes=(10000,))
        with self.assertRaisesRegex(module.R2ManagementError, "HTTP 403") as caught:
            self.service.refresh_usage(ACCOUNT)
        self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertTrue(self.storage.state["shutdown_ok"])
        self.assertEqual(self.cloud.calls.count(("managed", False)), 1)

    def test_b_limit_closes_actual_unregistered_domains_before_any_remote_write(self):
        self.cloud.domains["unregistered.example.test"] = {**self.cloud.domains[HOSTS[0]], "domain": "unregistered.example.test"}
        self.cloud.managed = True
        self.cloud.usage_b = 80
        self.service.refresh_usage(ACCOUNT)
        self.assertTrue(self.storage.state["access_paused"])
        self.assertTrue(self.storage.state["shutdown_ok"])
        self.assertTrue(all(self.cloud.gates_at_disable))
        self.assertFalse(self.cloud.managed)
        self.assertFalse(any(item["enabled"] for item in self.cloud.domains.values()))
        self.assertTrue(all(item["enabled"] for item in self.config.settings["r2_accounts"][0]["domains"]))
        self.cloud.usage_b = 1
        self.service.refresh_usage(ACCOUNT)
        self.assertTrue(self.storage.state["access_paused"], "falling counters must not restore automatically")

    def test_shutdown_failure_visible_and_retried_on_next_tick_even_disabled(self):
        self.cloud.usage_b = 80
        self.cloud.disable_failures.add(HOSTS[1])
        self.service.refresh_usage(ACCOUNT)
        self.assertFalse(self.storage.state["shutdown_ok"])
        self.assertIn(HOSTS[1], self.storage.state["domain_shutdown_errors"])
        self.assertNotIn("synthetic-private", self.storage.state["shutdown_error"])
        self.config.settings["r2_accounts"][0]["enabled"] = False
        self.cloud.disable_failures.clear()
        self.service.maintenance_tick()
        self.assertTrue(self.storage.state["shutdown_ok"])
        self.assertEqual(len(self.cloud.usage_calls), 1)
        self.assertEqual(self.storage.cleanup_count, 1)
        self.assertEqual(self.cloud.calls.count(("managed", False)), 2)
        calls = copy.deepcopy(self.cloud.calls)
        self.service.maintenance_tick()
        self.assertEqual(self.cloud.calls, calls, "confirmed closure must not be repeated")
        self.assertEqual(self.storage.cleanup_count, 2)

    def test_missing_credentials_still_persist_pause_and_error(self):
        self.storage.state["cloud_b"] = 80
        self.config.settings["r2_accounts"][0]["api_token"] = ""
        with self.assertRaises(module.R2ManagementError):
            self.service.refresh_usage(ACCOUNT)
        self.assertTrue(self.storage.state["access_paused"])
        self.assertFalse(self.storage.state["complete"])
        self.assertTrue(self.storage.state["error"])

    def test_failed_refresh_waits_normal_interval_and_does_not_starve_cleanup(self):
        self.cloud.usage_error = CloudflareAPIError("synthetic failure")
        self.storage.state.pop("fetched_at")
        first = self.service.maintenance_tick()
        second = self.service.maintenance_tick()
        self.assertFalse(first["accounts"][0]["ok"])
        self.assertTrue(second["accounts"][0]["ok"])
        self.assertEqual(len(self.cloud.usage_calls), 1)
        self.assertEqual(self.storage.cleanup_count, 2)
        self.assertTrue(self.storage.state["error"])

    def test_rules_preserve_user_rules_and_share_free_rate_rule_per_zone(self):
        originals = {phase: copy.deepcopy(entry["rules"][0]) for phase, entry in self.cloud.rulesets.items()}
        self.service.apply_protection(ACCOUNT, 120, True, ["https://app.example.test"])
        self.service.apply_protection(ACCOUNT, 140, True, ["https://app.example.test"])
        for phase, entry in self.cloud.rulesets.items():
            self.assertEqual(entry["rules"][0], originals[phase])
            self.assertEqual(len(entry["rules"]), 2)
        rate = self.cloud.rulesets["http_ratelimit"]["rules"][1]
        self.assertNotIn("http.host", rate["expression"])
        self.assertEqual(rate["ratelimit"], {"characteristics": ["cf.colo.id", "ip.src"], "period": 10,
                                             "requests_per_period": 140, "mitigation_timeout": 10})
        cache = self.cloud.rulesets["http_request_cache_settings"]["rules"][1]["action_parameters"]
        self.assertEqual(cache["edge_ttl"], {"mode": "respect_origin"})
        self.assertEqual(cache["browser_ttl"], {"mode": "respect_origin"})
        self.assertIn(("cors", ["https://app.example.test"]), self.cloud.calls)
        self.assertFalse(any(call[0] in {"PUT", "DELETE"} for call in self.cloud.calls))
        recorded = self.storage.state["protection_rules"][ZONE]["http_ratelimit"]
        self.assertEqual(recorded["rule_id"], "chatgpt2api-r2-images-rate-id")
        self.assertEqual(recorded["ruleset_id"], "http_ratelimit-set")

    def test_new_ruleset_only_when_missing_and_disabling_only_owned_methods(self):
        self.cloud.rulesets = {}
        self.service.apply_protection(ACCOUNT, 120, True, ["*"])
        self.assertEqual(len(self.cloud.rulesets), 3)
        self.service.apply_protection(ACCOUNT, 120, False, ["*"])
        self.assertFalse(self.cloud.rulesets["http_request_firewall_custom"]["rules"][0]["enabled"])
        creates = [call for call in self.cloud.calls if call[0] == "POST" and call[1].endswith("/rulesets")]
        self.assertEqual(len(creates), 3)

    def test_quota_conflict_is_clear_and_fails_closed_without_deleting_rules(self):
        self.cloud.rules_error = True
        with self.assertRaisesRegex(module.R2ManagementError, "免费规则名额"):
            self.service.apply_protection(ACCOUNT, 120, True, ["*"])
        self.assertFalse(self.storage.state["domains_safe"])
        self.assertEqual(self.storage.state["verified_domains"], [])
        self.assertFalse(any(item["enabled"] for item in self.cloud.domains.values()))
        self.assertNotIn("synthetic-private", self.storage.state["protection_error"])
        self.assertTrue(all(len(entry["rules"]) == 1 for entry in self.cloud.rulesets.values()))

    def test_protection_disables_unknown_bindings(self):
        self.cloud.domains["other.example.test"] = {**self.cloud.domains[HOSTS[0]], "domain": "other.example.test"}
        self.service.apply_protection(ACCOUNT, 120, True, ["*"])
        self.assertFalse(self.cloud.domains["other.example.test"]["enabled"])
        self.assertTrue(self.storage.state["domains_safe"])

    def test_restore_rechecks_fresh_budget_and_enables_only_intended(self):
        self.storage.state["access_paused"] = True
        self.cloud.domains["other.example.test"] = {**self.cloud.domains[HOSTS[0]], "domain": "other.example.test"}
        result = self.service.restore_access(ACCOUNT)
        self.assertTrue(result["ok"])
        self.assertFalse(self.storage.state["access_paused"])
        self.assertFalse(self.storage.state["shutdown_ok"])
        self.assertTrue(self.storage.state["domains_safe"])
        self.assertTrue(all(self.cloud.domains[host]["enabled"] for host in HOSTS))
        self.assertFalse(self.cloud.domains["other.example.test"]["enabled"])
        self.assertEqual(len(self.cloud.usage_calls), 1)

    def test_restore_above_budget_never_enables(self):
        self.storage.state["access_paused"] = True
        self.cloud.usage_b = 80
        with self.assertRaisesRegex(module.R2ManagementError, "B 类"):
            self.service.restore_access(ACCOUNT)
        self.assertFalse(any(call[0] == "domain" and call[2] is True for call in self.cloud.calls))
        self.assertTrue(self.storage.state["access_paused"])

    def test_partial_restore_failure_recloses_successfully_enabled_domain(self):
        self.storage.state["access_paused"] = True
        self.cloud.enable_failures.add(HOSTS[1])
        with self.assertRaises(module.R2ManagementError):
            self.service.restore_access(ACCOUNT)
        self.assertTrue(self.storage.state["access_paused"])
        self.assertFalse(any(item["enabled"] for item in self.cloud.domains.values()))
        self.assertTrue(self.storage.state["restore_error"])

    def test_manual_switch_is_audited_and_pause_cannot_be_bypassed(self):
        self.service.update_domain(ACCOUNT, HOSTS[1], {"active": True}, "synthetic-admin")
        self.assertEqual(self.config.settings["r2_accounts"][0]["active_domain_id"], HOSTS[1])
        self.logs.add.assert_called_once()
        self.storage.state["access_paused"] = True
        with self.assertRaisesRegex(module.R2ManagementError, "恢复公开访问"):
            self.service.update_domain(ACCOUNT, HOSTS[0], {"active": True})
        with self.assertRaises(module.R2ManagementError):
            self.service.update_domain(ACCOUNT, HOSTS[0], {"enabled": True})
        self.assertEqual(self.cloud.usage_calls, [])

    def test_switch_and_disable_cannot_be_combined_and_failed_disable_invalidates_status(self):
        with self.assertRaises(module.R2ManagementError):
            self.service.update_domain(ACCOUNT, HOSTS[0], {"active": True, "enabled": False})
        self.assertEqual(self.cloud.calls, [])
        self.cloud.disable_failures.add(HOSTS[1])
        with self.assertRaises(CloudflareAPIError):
            self.service.update_domain(ACCOUNT, HOSTS[1], {"enabled": False})
        self.assertFalse(self.storage.state["domains_safe"])
        self.assertFalse(self.storage.state["domains_checked"])

    def test_current_domain_delete_requires_switch_or_disabled_account(self):
        with self.assertRaises(module.R2ManagementError):
            self.service.delete_domain(ACCOUNT, HOSTS[0])
        self.config.settings["r2_accounts"][0]["enabled"] = False
        self.service.delete_domain(ACCOUNT, HOSTS[0])
        self.assertNotIn(HOSTS[0], self.cloud.domains)
        self.assertEqual(self.config.settings["r2_accounts"][0]["active_domain_id"], "")

    def test_settings_cannot_bypass_checked_manual_switch(self):
        settings = self.config.get_image_storage_settings()
        settings["r2_accounts"][0]["active_domain_id"] = HOSTS[1]
        with self.assertRaisesRegex(module.R2ManagementError, "手动切换"):
            self.service.save_settings(settings)
        self.assertEqual(self.config.writes, 0)
        self.assertEqual(self.config.settings["r2_accounts"][0]["active_domain_id"], HOSTS[0])

    def test_identity_change_invalidates_old_usage_and_protection(self):
        settings = self.config.get_image_storage_settings()
        settings["r2_accounts"][0]["bucket"] = "another-synthetic-bucket"
        self.service.save_settings(settings)
        self.assertFalse(self.storage.state["complete"])
        self.assertFalse(self.storage.state["domains_safe"])
        self.assertEqual(self.storage.state["verified_domains"], [])
        self.assertEqual(self.cloud.calls, [])

    def test_add_starts_disabled_and_requires_same_account_zone(self):
        before = self.config.writes
        with self.assertRaises(module.R2ManagementError):
            self.service.add_domain(ACCOUNT, "outside.invalid", ZONE)
        self.assertEqual(self.config.writes, before)
        result = self.service.add_domain(ACCOUNT, "new.example.test", ZONE)
        self.assertFalse(result["enabled"])
        self.assertIn(("add", "new.example.test", False), self.cloud.calls)

    def test_connection_list_is_debited_even_if_it_fails(self):
        client = mock.Mock()
        client.test_connection.side_effect = R2Error("synthetic failure")
        with mock.patch.object(module, "CloudflareR2Client", return_value=client):
            with self.assertRaises(R2Error):
                self.service.test_connection(ACCOUNT)
        self.assertEqual(self.storage.state["a_local"], 16)
        self.assertIn({"a_local_increment": 1}, self.storage.updates)
        client.close.assert_called_once()

    def test_check_without_sample_or_when_paused_never_issues_head(self):
        with mock.patch.object(self.service, "_head") as head:
            result = self.service.check_domains(ACCOUNT, HOSTS[0])
            self.assertIn("无未过期图片", result[0]["error"])
            self.storage.state["access_paused"] = True
            result = self.service.check_domains(ACCOUNT, HOSTS[0])
            self.assertIn("额度停读", result[0]["error"])
            head.assert_not_called()

    def test_check_uses_only_selected_configured_host_and_unexpired_sample(self):
        self.storage.items = [{"storage": "r2", "account_id": ACCOUNT, "object_key": "images/2026/06/12/fixture.png",
                               "expires_at": module._iso(NOW + timedelta(minutes=10)), "pending": False}]
        with mock.patch.object(self.service, "_head", return_value={"ok": True}) as head:
            result = self.service.check_domains(ACCOUNT, HOSTS[1])
            head.assert_called_once_with("https://backup.example.test/images/2026/06/12/fixture.png")
        self.assertEqual(result[0]["source"], "server")
        self.assertEqual(self.config.settings["r2_accounts"][0]["active_domain_id"], HOSTS[0])

    def test_head_checks_server_network_with_tls_without_following_redirect(self):
        session = mock.Mock()
        session.request.return_value.status_code = 302
        with mock.patch.object(module.requests, "Session", return_value=session) as factory:
            result = self.service._head("https://images.example.test/images/fixture.png")
        self.assertFalse(result["ok"])
        factory.assert_called_once_with(verify=True, trust_env=False)
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])
        session.close.assert_called_once()

    def test_disabled_account_with_existing_images_still_monitors_b_budget(self):
        self.config.settings["r2_accounts"][0]["enabled"] = False
        self.storage.items = [{"storage": "r2", "account_id": ACCOUNT}]
        self.storage.state.pop("fetched_at")
        self.cloud.usage_b = 80
        self.service.maintenance_tick()
        self.assertTrue(self.storage.state["access_paused"])
        self.assertTrue(self.storage.state["shutdown_ok"])

    def test_resolve_whitelists_public_reference_fields(self):
        self.storage.resolve_reference = mock.Mock(return_value={"image_id": "images/fixture.png", "account_id": ACCOUNT,
                                                                 "url": "", "expired": True, "api_token": "synthetic-secret"})
        result = self.service.resolve_images([{"image_id": "images/fixture.png"}])
        self.assertEqual(result, [{"image_id": "images/fixture.png", "account_id": ACCOUNT, "url": "", "expired": True}])


if __name__ == "__main__":
    unittest.main()
