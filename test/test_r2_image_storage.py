"""Synthetic/mocked R2 storage tests; run only through test/run_isolated.py."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

from services import config as config_module
from services import image_storage_service as storage
from services import r2_provision
from services.image_storage_service import ImageStorageError, ImageStorageService
from services.r2_client import R2Error


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2)).save(output, format="PNG")
    return output.getvalue()


def account(index: int, limit: int = 10) -> dict:
    return {
        "id": f"00000000-0000-4000-8000-00000000000{index}", "name": f"account {index}", "enabled": True,
        "account_id": str(index) * 32, "bucket": f"images-{index}",
        "access_key_id": "synthetic-access", "secret_access_key": "synthetic-secret",
        "public_base_url": f"https://img{index}.example.test", "monthly_upload_limit": limit,
    }


class SyntheticConfig:
    def __init__(self, settings: dict):
        self.settings = settings
        self.base_url = "https://app.example.test"

    def get_image_storage_settings(self):
        return deepcopy(self.settings)


class R2ImageStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        self.settings = {"mode": "r2", "enabled": True, "retention_minutes": 60, "cache_ttl_seconds": 60,
                         "r2_accounts": [account(1), account(2, limit=20)]}
        for name, value in (("DATA_DIR", self.root), ("config", SyntheticConfig(self.settings))):
            patcher = mock.patch.object(storage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clock = mock.patch.object(storage, "_utc_now", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        factory = mock.patch.object(storage, "CloudflareR2Client")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = self.factory.return_value
        self.service = ImageStorageService(self.root / "image_index.json", self.root / "r2_usage.json")

    def usage(self) -> dict:
        return json.loads((self.root / "r2_usage.json").read_text())

    def write_usage(self, data: dict) -> None:
        (self.root / "r2_usage.json").write_text(json.dumps(data))

    def test_upload_returns_account_public_url_and_counts_upload(self):
        stored = self.service.save(image_bytes())
        first = self.settings["r2_accounts"][0]
        self.assertEqual(stored.account_id, first["id"])
        self.assertEqual(stored.url, f"{first['public_base_url']}/{stored.rel}")
        self.assertEqual(self.usage(), {first["id"]: {"month": "2026-05", "uploads": 1}})
        self.client.upload_bytes.assert_called_once()
        self.assertFalse(self.service.metadata(stored.rel)["pending"])

    def test_picks_lowest_used_share_and_skips_exhausted_or_disabled(self):
        one, two = (item["id"] for item in self.settings["r2_accounts"])
        # 5/10 = 50% vs 4/20 = 20%: the larger account is less used.
        self.write_usage({one: {"month": "2026-05", "uploads": 5}, two: {"month": "2026-05", "uploads": 4}})
        self.assertEqual(self.service.save(image_bytes()).account_id, two)
        self.write_usage({one: {"month": "2026-05", "uploads": 1}, two: {"month": "2026-05", "uploads": 20}})
        self.assertEqual(self.service.save(image_bytes()).account_id, one)
        self.settings["r2_accounts"][0]["enabled"] = False
        with self.assertRaises(ImageStorageError):
            self.service.save(image_bytes())
        with self.assertRaises(ImageStorageError):
            self.service.require_upload_available()

    def test_previous_month_usage_does_not_count(self):
        one, two = (item["id"] for item in self.settings["r2_accounts"])
        self.write_usage({one: {"month": "2026-04", "uploads": 10}, two: {"month": "2026-05", "uploads": 1}})
        self.assertEqual(self.service.save(image_bytes()).account_id, one)
        self.assertEqual(self.usage()[one], {"month": "2026-05", "uploads": 1})

    def test_failed_upload_is_still_counted_and_kept_pending_for_cleanup(self):
        self.client.upload_bytes.side_effect = R2Error("synthetic")
        with self.assertRaises(ImageStorageError):
            self.service.save(image_bytes())
        self.assertEqual(sum(item["uploads"] for item in self.usage().values()), 1)
        items = list(json.loads((self.root / "image_index.json").read_text())["items"].values())
        self.assertTrue(items[0]["pending"])

    def test_expired_images_are_deleted_with_their_account(self):
        stored = self.service.save(image_bytes())
        self.now += timedelta(minutes=61)
        self.assertEqual(self.service.cleanup_expired(), {"removed": 1, "failed": 0, "pending": 0})
        self.client.delete_object.assert_called_once_with(stored.rel)
        self.assertEqual(self.factory.call_args_list[-1].args[0]["id"], stored.account_id)
        self.assertIsNone(self.service.metadata(stored.rel))

    def test_resolve_reference_marks_expired(self):
        stored = self.service.save(image_bytes())
        ref = {"image_id": stored.rel, "account_id": stored.account_id}
        self.assertEqual(self.service.resolve_reference(ref)["url"], stored.url)
        self.now += timedelta(minutes=61)
        self.assertTrue(self.service.resolve_reference(ref)["expired"])

    def test_summaries_and_connection_test_count_operations(self):
        one, two = (item["id"] for item in self.settings["r2_accounts"])
        self.client.test_connection.return_value = {"ok": True, "status": 200}
        self.assertTrue(self.service.test_account(one)["ok"])
        # The connection test made account one 1/10 used, so the upload goes to two (0/20).
        self.assertEqual(self.service.save(image_bytes()).account_id, two)
        summaries = {item["id"]: item for item in self.service.account_summaries()}
        self.assertEqual(summaries[one], {"id": one, "month": "2026-05", "monthly_uploads": 1, "image_count": 0})
        self.assertEqual(summaries[two], {"id": two, "month": "2026-05", "monthly_uploads": 1, "image_count": 1})


class R2ConfigTests(unittest.TestCase):
    def normalize(self, accounts: list[dict]) -> dict:
        settings = config_module._normalize_image_storage_settings({"enabled": True, "mode": "r2", "r2_accounts": accounts})
        config_module._validate_image_storage_settings(settings)
        return settings

    def test_minimal_account_and_defaults(self):
        source = {key: value for key, value in account(1).items() if key != "monthly_upload_limit"}
        source["public_base_url"] += "/"
        normalized = self.normalize([source])["r2_accounts"][0]
        self.assertEqual(normalized["monthly_upload_limit"], 900_000)
        self.assertEqual(normalized["public_base_url"], "https://img1.example.test")

    def test_enabled_account_requires_public_base_url(self):
        with self.assertRaisesRegex(ValueError, "public_base_url"):
            self.normalize([{**account(1), "public_base_url": ""}])
        with self.assertRaisesRegex(ValueError, "public_base_url"):
            self.normalize([{**account(1), "public_base_url": "img.example.test"}])
        self.normalize([{**account(1), "enabled": False, "public_base_url": ""}])

    def test_blank_secrets_keep_previous_values(self):
        previous = self.normalize([account(1)])
        merged = config_module._merge_image_storage_settings(
            previous, {"r2_accounts": [{**account(1), "access_key_id": "", "secret_access_key": ""}]},
        )
        self.assertEqual(merged["r2_accounts"][0]["secret_access_key"], "synthetic-secret")


class R2ProvisionTests(unittest.TestCase):
    def run_provision(self, responses: dict):
        calls = []

        def request(method, url, **kwargs):
            path = url.removeprefix(r2_provision._BASE_URL)
            calls.append((method, path, kwargs.get("json") or kwargs.get("params")))
            status, result = responses.get((method, path), (200, []))
            return mock.Mock(status_code=status, json=lambda: {
                "success": status < 300, "result": result, "errors": [{"code": 10000, "message": "synthetic"}]})

        with mock.patch.object(r2_provision.requests, "Session") as session:
            session.return_value.request.side_effect = request
            return r2_provision.provision(" synthetic-token ", "Img.Example.Test"), calls

    def test_creates_bucket_binds_domain_and_derives_s3_credentials(self):
        cf_id = "a" * 32
        zone = {"id": "zone-1", "name": "example.test", "account": {"id": cf_id}}
        result, calls = self.run_provision({
            ("GET", "/user/tokens/verify"): (200, {"id": "token-id"}),
            ("GET", "/zones"): (200, [zone]),
            # Bucket left over from an earlier attempt is reused.
            ("POST", f"/accounts/{cf_id}/r2/buckets"): (409, None),
        })
        self.assertEqual(result, {
            "name": "img.example.test", "enabled": True, "account_id": cf_id, "bucket": "img-example-test",
            "access_key_id": "token-id", "secret_access_key": hashlib.sha256(b"synthetic-token").hexdigest(),
            "public_base_url": "https://img.example.test",
        })
        self.assertEqual(calls[-1], ("POST", f"/accounts/{cf_id}/r2/buckets/img-example-test/domains/custom",
                                     {"domain": "img.example.test", "zoneId": "zone-1", "enabled": True}))
        config_module._validate_image_storage_settings(config_module._normalize_image_storage_settings(
            {"r2_accounts": [{"id": account(1)["id"], **result}]}))

    def test_cloudflare_error_and_unknown_zone_are_reported(self):
        with self.assertRaisesRegex(ImageStorageError, "10000 synthetic"):
            self.run_provision({("GET", "/user/tokens/verify"): (403, None)})
        with self.assertRaisesRegex(ImageStorageError, "站点"):
            self.run_provision({("GET", "/user/tokens/verify"): (200, {"id": "token-id"})})
        with self.assertRaises(ValueError):
            r2_provision.provision("token", "not a domain")


if __name__ == "__main__":
    unittest.main()
