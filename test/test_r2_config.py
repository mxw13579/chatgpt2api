"""Synthetic config fixtures; run only via test/run_isolated.py.

Storage validation is mocked: these tests cover config's delegation/atomicity,
not B's object-index implementation or real Cloudflare access.
"""
from __future__ import annotations

import builtins
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import config as config_module
from services import image_storage_service as storage_module


ACCOUNT_A = "00000000-0000-4000-8000-000000000001"
ACCOUNT_B = "00000000-0000-4000-8000-000000000002"
SECRETS = ("access_key_id", "secret_access_key", "api_token")


def account(**changes) -> dict:
    return {
        "id": ACCOUNT_A,
        "name": "Synthetic account",
        "enabled": True,
        "account_id": "a" * 32,
        "bucket": "fixture-images",
        "access_key_id": "synthetic-access-a",
        "secret_access_key": "synthetic-secret-a",
        "api_token": "synthetic-token-a",
        **changes,
    }


def domain(hostname: str = "images.example.test", **changes) -> dict:
    return {"id": hostname, "domain": hostname, "zone_id": "c" * 32, "enabled": False, **changes}


def settings(*accounts, **changes) -> dict:
    return {"enabled": True, "mode": "r2", "r2_accounts": list(accounts), **changes}


class R2ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="r2-config-fixture-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for patcher in (
            mock.patch.object(config_module, "DATA_DIR", self.root / "data"),
            mock.patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": "synthetic-config-auth"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = self.root / "config.json"
        self.path.write_text("{}\n", encoding="utf-8")
        self.store = config_module.ConfigStore(self.path)
        self.storage = mock.Mock(spec=["validate_settings_change"])
        patcher = mock.patch.object(storage_module, "image_storage_service", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, image_settings: dict) -> None:
        self.store.data["image_storage"] = copy.deepcopy(image_settings)
        self.store._save()

    def assert_rejected(self, image_settings: object, message: str) -> ValueError:
        before = copy.deepcopy(self.store.data)
        persisted = self.path.read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, message) as raised:
            self.store.update({"image_storage": image_settings})
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.path.read_text(encoding="utf-8"), persisted)
        return raised.exception

    def test_defaults_and_getter_copies(self) -> None:
        private = self.store.get_image_storage_settings()
        self.assertEqual(private, config_module.DEFAULT_IMAGE_STORAGE)
        self.assertEqual(private["retention_minutes"], 60)
        self.assertEqual(private["cleanup_interval_seconds"], 60)
        self.assertEqual(private["usage_refresh_seconds"], 300)
        self.assertEqual(private["usage_max_age_seconds"], 900)
        self.assertEqual(private["cache_ttl_seconds"], 60)
        private["cors_origins"].clear()
        private["r2_accounts"].append({"id": "not-stored"})
        self.assertEqual(self.store.get_public_image_storage_settings(), config_module.DEFAULT_IMAGE_STORAGE)
        self.assertEqual(config_module.DEFAULT_IMAGE_STORAGE["cors_origins"], ["*"])
        self.assertEqual(config_module.DEFAULT_IMAGE_STORAGE["r2_accounts"], [])

    def test_r2_defaults_and_canonical_fields_are_persisted(self) -> None:
        candidate = account(
            id=ACCOUNT_A.upper(), name=" Synthetic account ", account_id=" AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ",
            bucket=" fixture-images ", capacity_bytes="8000000000", billing_cycle_day="28",
            billing_cycle_confirmed="yes", restrict_methods="false",
            domains=[domain(" IMAGES.EXAMPLE.TEST ", zone_id="C" * 32, enabled="true")],
            active_domain_id=" IMAGES.EXAMPLE.TEST ", prefix="not-an-object-prefix",
            has_access_key_id=True,
        )
        self.store.update({"image_storage": settings(candidate, mode=" R2 ", retention_minutes="15", prefix="ignored")})
        private = self.store.get_image_storage_settings()
        saved = private["r2_accounts"][0]
        self.assertEqual(private["mode"], "r2")
        self.assertEqual(private["retention_minutes"], 15)
        self.assertEqual(saved["id"], ACCOUNT_A)
        self.assertEqual(saved["name"], "Synthetic account")
        self.assertEqual(saved["account_id"], "a" * 32)
        self.assertEqual(saved["bucket"], "fixture-images")
        self.assertEqual(saved["capacity_bytes"], 8_000_000_000)
        self.assertEqual(saved["a_budget"], 800_000)
        self.assertEqual(saved["b_warn"], 7_000_000)
        self.assertEqual(saved["b_budget"], 8_000_000)
        self.assertEqual(saved["billing_cycle_day"], 28)
        self.assertTrue(saved["billing_cycle_confirmed"])
        self.assertEqual(saved["rate_limit_requests"], 120)
        self.assertFalse(saved["restrict_methods"])
        self.assertEqual(saved["active_domain_id"], "images.example.test")
        self.assertEqual(saved["domains"], [domain(enabled=True)])
        self.assertNotIn("prefix", private)
        self.assertNotIn("prefix", saved)
        self.assertNotIn("has_access_key_id", saved)
        reloaded = config_module.ConfigStore(self.path)
        self.assertEqual(reloaded.get_image_storage_settings(), private)

    def test_local_and_webdav_normalization_remains_compatible(self) -> None:
        for mode, enabled, expected in (("local", False, "local"), ("webdav", False, "local"), ("webdav", True, "webdav"), ("both", True, "both")):
            with self.subTest(mode=mode, enabled=enabled):
                result = self.store.update({"image_storage": {
                    "mode": mode, "enabled": enabled, "webdav_url": " https://dav.example.test/ ",
                    "webdav_username": " fixture-user ", "webdav_password": " synthetic-dav-password ",
                    "webdav_root_path": " /fixtures/images/ ", "public_base_url": " https://cdn.example.test/images/ ",
                }})["image_storage"]
                self.assertEqual(result["mode"], expected)
                self.assertEqual(result["webdav_url"], "https://dav.example.test")
                self.assertEqual(result["webdav_username"], "fixture-user")
                self.assertEqual(result["webdav_password"], "synthetic-dav-password")
                self.assertEqual(result["webdav_root_path"], "fixtures/images")
                self.assertEqual(result["public_base_url"], "https://cdn.example.test/images")

    def test_webdav_required_fields_do_not_apply_to_r2(self) -> None:
        self.store.update({"image_storage": settings(account())})
        self.assertEqual(self.store.get_image_storage_settings()["webdav_url"], "")
        for mode in ("webdav", "both"):
            with self.subTest(mode=mode):
                self.assert_rejected({"mode": mode}, "WebDAV URL")
                self.assert_rejected({"mode": mode, "webdav_url": "https://dav.example.test"}, "WebDAV 密码")

    def test_r2_cannot_silently_fall_back_to_local(self) -> None:
        self.assert_rejected(settings(account(), enabled=False), "enabled=true")
        self.assert_rejected({"mode": "r2"}, "enabled=true")
        self.seed(settings(account(), enabled=False))
        self.assertEqual(self.store.get_image_storage_settings()["mode"], "r2")
        self.storage.validate_settings_change.assert_not_called()

    def test_multiple_disabled_incomplete_drafts_are_allowed(self) -> None:
        self.store.update({"image_storage": settings(
            {"id": ACCOUNT_A, "enabled": False}, {"id": ACCOUNT_B, "enabled": False},
        )})
        for draft in self.store.get_public_image_storage_settings()["r2_accounts"]:
            self.assertFalse(draft["enabled"])
            self.assertFalse(draft["billing_cycle_confirmed"])
            self.assertEqual(draft["billing_cycle_day"], 1)
            self.assertEqual(draft["domains"], [])
            self.assertEqual(draft["active_domain_id"], "")
            for field in SECRETS:
                self.assertEqual(draft[field], "")
                self.assertFalse(draft[f"has_{field}"])
        self.assert_rejected({"r2_accounts": [{"id": ACCOUNT_A, "enabled": True}]}, "account_id")

    def test_enabled_accounts_require_local_credentials_but_not_cloud_verification(self) -> None:
        for field in ("account_id", "bucket", *SECRETS):
            with self.subTest(field=field):
                self.assert_rejected(settings(account(**{field: ""})), field)
        self.storage.validate_settings_change.assert_not_called()
        self.store.update({"image_storage": settings(account())})
        self.storage.validate_settings_change.assert_called_once()
        saved = self.store.get_image_storage_settings()["r2_accounts"][0]
        self.assertFalse(saved["billing_cycle_confirmed"])
        self.assertEqual(saved["domains"], [])

    def test_invalid_collection_shapes_are_rejected_without_resetting_config(self) -> None:
        cases = [
            (None, "image_storage"), ([], "image_storage"),
            ({"r2_accounts": None}, "r2_accounts"), ({"r2_accounts": {}}, "r2_accounts"),
            ({"r2_accounts": [None]}, "账号必须是对象"),
            (settings(account(domains=None)), "domains"),
            (settings(account(domains=[None])), "域名必须是对象"),
        ]
        for candidate, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(candidate, message)
        self.storage.validate_settings_change.assert_not_called()

    def test_identity_and_bucket_boundaries(self) -> None:
        for field, value in (
            ("id", ""), ("id", "fixture-not-uuid"), ("account_id", "short"),
            ("account_id", "g" * 32), ("bucket", "UPPERCASE"), ("bucket", "ab"),
            ("bucket", "fixture/images"), ("bucket", "-fixture"), ("bucket", "x" * 64),
            ("enabled", "invalid-bool"), ("name", {"invalid": "shape"}),
        ):
            with self.subTest(field=field, value=value):
                self.assert_rejected(settings(account(**{field: value})), field)

    def test_duplicate_internal_and_cf_ids_are_rejected(self) -> None:
        self.assert_rejected(settings(account(), account(account_id="b" * 32)), "账号 id 不得重复")
        self.assert_rejected(settings(account(), account(id=ACCOUNT_B, account_id="A" * 32)), "CF Account ID 不得重复")
        self.storage.validate_settings_change.assert_not_called()

    def test_positive_limits_are_strict_integers(self) -> None:
        for field in ("capacity_bytes", "a_budget", "b_budget", "rate_limit_requests"):
            for value in (0, -1, True, 1.5, "invalid-number", None):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(settings(account(**{field: value})), field)
        for field in ("retention_minutes", "cleanup_interval_seconds", "usage_refresh_seconds", "usage_max_age_seconds"):
            for value in (0, -1, False, 1.5, "invalid-number", None):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(settings(account(), **{field: value}), field)
        self.storage.validate_settings_change.assert_not_called()

    def test_cycle_warning_and_cache_limits(self) -> None:
        for day in (0, 29, 1.5, True):
            with self.subTest(day=day):
                self.assert_rejected(settings(account(billing_cycle_day=day)), "billing_cycle_day")
        self.assert_rejected(settings(account(b_warn=-1)), "b_warn")
        self.assert_rejected(settings(account(b_warn=8_000_001)), "b_warn")
        self.assert_rejected(settings(account(), cache_ttl_seconds=-1), "cache_ttl_seconds")
        self.store.update({"image_storage": settings(account(b_warn=0, billing_cycle_day=28), cache_ttl_seconds=0)})
        self.assertEqual(self.store.get_image_storage_settings()["cache_ttl_seconds"], 0)

    def test_domains_must_be_hostnames_not_urls_paths_or_ip_addresses(self) -> None:
        for hostname in (
            "https://images.example.test", "images.example.test/path", "images.example.test:443",
            "127.0.0.1", "01.2.3.4", "[::1]", "::1", "localhost", "*.example.test",
            "user@images.example.test", "images.example.test?key=value", "bad_name.example.test",
            "-bad.example.test", "images..example.test", "images.example.test.",
            "images\\example.test", "bad\nname.example.test", "x" * 64 + ".example.test",
        ):
            with self.subTest(hostname=hostname):
                self.assert_rejected(settings(account(domains=[domain(hostname)])), "主机名")
        self.storage.validate_settings_change.assert_not_called()

    def test_domain_identity_zone_active_and_uniqueness(self) -> None:
        self.assert_rejected(settings(account(domains=[domain(id="other.example.test")])), "domain id")
        self.assert_rejected(settings(account(domains=[domain(zone_id="not-a-zone")])), "zone_id")
        self.assert_rejected(settings(account(active_domain_id="missing.example.test")), "active_domain_id")
        self.assert_rejected(settings(account(domains=[domain(), domain("IMAGES.EXAMPLE.TEST")])), "domain 不得重复")
        self.assert_rejected(settings(
            account(domains=[domain()]), account(id=ACCOUNT_B, account_id="b" * 32, domains=[domain()]),
        ), "domain 不得重复")
        self.assert_rejected(settings(account(domains=[domain(f"images-{i}.example.test") for i in range(101)])), "100")
        # A disabled binding may remain selected while quota protection pauses access.
        self.store.update({"image_storage": settings(account(domains=[domain()], active_domain_id="images.example.test"))})

    def test_cors_origins_are_normalized_without_network(self) -> None:
        self.store.update({"image_storage": settings(account(), cors_origins=[
            " https://APP.EXAMPLE.TEST/ ", "https://app.example.test", "http://localhost:3000",
        ])})
        self.assertEqual(self.store.get_image_storage_settings()["cors_origins"], [
            "https://app.example.test", "http://localhost:3000",
        ])
        for value in ("*", None, [None], ["javascript:bad"], ["https://user:password@example.test"], ["https://example.test/path"], ["https://example.test:99999"]):
            with self.subTest(value=value):
                self.assert_rejected({"cors_origins": value}, "CORS|cors_origins")

    def test_all_public_get_paths_redact_raw_stored_secrets_without_mutation(self) -> None:
        raw = settings(account(domains=[domain()]))
        self.seed(raw)
        before = copy.deepcopy(self.store.data)
        full_response = self.store.get()
        for field in SECRETS:
            self.assertNotIn(raw["r2_accounts"][0][field], json.dumps(full_response))
        for public in (self.store.get_public_image_storage_settings(), full_response["image_storage"]):
            serialized = json.dumps(public)
            for field in SECRETS:
                self.assertNotIn(raw["r2_accounts"][0][field], serialized)
                self.assertEqual(public["r2_accounts"][0][field], "")
                self.assertTrue(public["r2_accounts"][0][f"has_{field}"])
            public["r2_accounts"][0]["domains"].clear()
        self.assertEqual(self.store.data, before)
        for field in SECRETS:
            self.assertEqual(self.store.get_image_storage_settings()["r2_accounts"][0][field], raw["r2_accounts"][0][field])
        self.storage.validate_settings_change.assert_not_called()

    def test_blank_omitted_and_null_secrets_are_preserved_by_id_not_position(self) -> None:
        other = account(id=ACCOUNT_B, account_id="b" * 32, **{field: f"synthetic-b-{field}" for field in SECRETS})
        self.seed(settings(account(), other))
        response = self.store.update({"image_storage": {"r2_accounts": [
            {"id": ACCOUNT_B, "access_key_id": "", "secret_access_key": "  ", "api_token": None},
            {"id": ACCOUNT_A, "name": "Renamed fixture", "has_api_token": False},
        ]}})
        saved = self.store.get_image_storage_settings()["r2_accounts"]
        self.assertEqual([item["id"] for item in saved], [ACCOUNT_B, ACCOUNT_A])
        for actual, original in zip(saved, (other, account())):
            for field in SECRETS:
                self.assertEqual(actual[field], original[field])
                self.assertNotIn(f"has_{field}", actual)
                self.assertNotIn(original[field], json.dumps(response))
        for public in response["image_storage"]["r2_accounts"]:
            for field in SECRETS:
                self.assertTrue(public[f"has_{field}"])
        persisted = json.loads(self.path.read_text(encoding="utf-8"))["image_storage"]["r2_accounts"]
        self.assertEqual(persisted, saved)
        self.assertEqual(saved[1]["name"], "Renamed fixture")

    def test_public_settings_round_trip_keeps_credentials(self) -> None:
        self.seed(settings(account()))
        public = self.store.get()
        unchanged = copy.deepcopy(public)
        response = self.store.update(public)
        self.assertEqual(public, unchanged)
        for field in SECRETS:
            self.assertEqual(self.store.get_image_storage_settings()["r2_accounts"][0][field], account()[field])
            self.assertEqual(response["image_storage"]["r2_accounts"][0][field], "")

    def test_new_account_id_does_not_inherit_previous_credentials(self) -> None:
        self.seed(settings(account()))
        self.store.update({"image_storage": {"r2_accounts": [{
            "id": ACCOUNT_B, "enabled": False, "account_id": "a" * 32, "bucket": "fixture-images",
        }]}})
        new = self.store.get_image_storage_settings()["r2_accounts"][0]
        for field in SECRETS:
            self.assertEqual(new[field], "")

    def test_mask_strings_are_rejected_and_not_saved(self) -> None:
        self.seed(settings(account()))
        for field in SECRETS:
            with self.subTest(field=field):
                self.assert_rejected({"r2_accounts": [{"id": ACCOUNT_A, field: "********"}]}, "密钥掩码")
        self.assertNotIn("********", self.path.read_text(encoding="utf-8"))
        self.storage.validate_settings_change.assert_not_called()

    def test_general_and_partial_legacy_saves_keep_r2_settings(self) -> None:
        self.seed(settings(account(), retention_minutes=17, cors_origins=["https://app.example.test"]))
        before = copy.deepcopy(self.store.data["image_storage"])
        self.store.update({"image_retention_days": 12})
        self.assertEqual(self.store.data["image_storage"], before)
        self.storage.validate_settings_change.assert_not_called()
        self.store.update({"image_storage": {"webdav_username": "fixture-user"}})
        private = self.store.get_image_storage_settings()
        self.assertEqual(private["mode"], "r2")
        self.assertTrue(private["enabled"])
        self.assertEqual(private["retention_minutes"], 17)
        self.assertEqual(private["cors_origins"], ["https://app.example.test"])
        self.assertEqual(private["r2_accounts"][0]["id"], ACCOUNT_A)

    def test_switching_modes_does_not_erase_accounts(self) -> None:
        self.seed(settings(account()))
        for patch in (
            {"enabled": False, "mode": "local"},
            {"enabled": True, "mode": "webdav", "webdav_url": "https://dav.example.test", "webdav_password": "synthetic-dav"},
            {"mode": "r2"},
        ):
            self.store.update({"image_storage": patch})
            saved = self.store.get_image_storage_settings()["r2_accounts"][0]
            self.assertEqual(saved["id"], ACCOUNT_A)
            for field in SECRETS:
                self.assertEqual(saved[field], account()[field])

    def test_storage_import_is_lazy_for_load_get_and_unrelated_update(self) -> None:
        self.seed(settings(account()))
        real_import = builtins.__import__

        def forbid_storage_import(name, *args, **kwargs):
            if name == "services.image_storage_service":
                raise AssertionError("Storage must not be imported here")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=forbid_storage_import):
            loaded = config_module.ConfigStore(self.path)
            loaded.get()
            loaded.get_image_storage_settings()
            loaded.get_public_image_storage_settings()
            loaded.update({"image_retention_days": 11})
        self.storage.validate_settings_change.assert_not_called()

    def test_validator_receives_full_private_candidate_before_store_mutation(self) -> None:
        self.seed(settings(account()))
        before = copy.deepcopy(self.store.data)

        def validate(candidate):
            self.assertEqual(self.store.data, before)
            self.assertEqual(candidate["mode"], "r2")
            self.assertEqual(candidate["retention_minutes"], 25)
            saved = candidate["r2_accounts"][0]
            self.assertEqual(saved["access_key_id"], account()["access_key_id"])
            self.assertEqual(saved["secret_access_key"], "synthetic-rotated-secret")
            self.assertEqual(saved["api_token"], account()["api_token"])
            self.assertFalse(saved["enabled"])

        self.storage.validate_settings_change.side_effect = validate
        self.store.update({"image_storage": {
            "retention_minutes": "25",
            "r2_accounts": [{"id": ACCOUNT_A, "enabled": False, "secret_access_key": "synthetic-rotated-secret"}],
        }})
        self.storage.validate_settings_change.assert_called_once()

    def test_outstanding_object_rejections_propagate_without_changing_memory_or_disk(self) -> None:
        self.seed(settings(account()))
        changes = (
            {"r2_accounts": []},
            {"r2_accounts": [account(account_id="b" * 32)]},
            {"r2_accounts": [account(bucket="replacement-images")]},
            {"r2_accounts": [account(id=ACCOUNT_B)]},
        )
        for number, candidate in enumerate(changes):
            with self.subTest(change=number):
                # B owns lookup of both pending reservations and uploaded objects.
                self.storage.validate_settings_change.reset_mock()
                self.storage.validate_settings_change.side_effect = ValueError("存在未删除的 R2 对象")
                self.assert_rejected(candidate, "未删除")
                self.storage.validate_settings_change.assert_called_once()
        self.storage.validate_settings_change.side_effect = None
        self.store.update({"image_storage": {"r2_accounts": []}})
        self.assertEqual(self.store.get_image_storage_settings()["r2_accounts"], [])

    def test_validator_io_errors_are_not_swallowed_or_saved(self) -> None:
        self.seed(settings(account()))
        before = copy.deepcopy(self.store.data)
        persisted = self.path.read_text(encoding="utf-8")
        self.storage.validate_settings_change.side_effect = OSError("synthetic index failure")
        with self.assertRaisesRegex(OSError, "synthetic index failure"):
            self.store.update({"image_storage": {"retention_minutes": 20}})
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.path.read_text(encoding="utf-8"), persisted)

    def test_validation_messages_do_not_echo_invalid_values_or_credentials(self) -> None:
        marker = "synthetic-sensitive-invalid-value"
        for candidate in (
            settings(account(capacity_bytes=marker)),
            settings(account(account_id=marker)),
            settings(account(domains=[domain(f"https://{marker}.example.test")])),
            settings(account(domains=[domain(zone_id=marker)])),
        ):
            error = self.assert_rejected(candidate, "R2|图片存储")
            self.assertNotIn(marker, str(error))
            for field in SECRETS:
                self.assertNotIn(account()[field], str(error))


if __name__ == "__main__":
    unittest.main()
