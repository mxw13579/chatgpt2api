"""Synthetic/mocked storage tests; run only through test/run_isolated.py.

These tests do not verify real Cloudflare authentication, accounting or caching.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest import mock

from fastapi import HTTPException
from PIL import Image

from services import image_storage_service as storage
from services.image_storage_service import ImageStorageError, ImageStorageService
from services.r2_client import R2Error


def image_bytes(fmt: str = "PNG") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), color=(20, 40, 60)).save(output, format=fmt)
    return output.getvalue()


def account_fixture() -> dict:
    return {
        "id": "00000000-0000-4000-8000-000000000001", "name": "synthetic account",
        "account_id": "1" * 32, "bucket": "synthetic-images", "enabled": True,
        "access_key_id": "synthetic-access", "secret_access_key": "synthetic-secret",
        "api_token": "synthetic-token", "capacity_bytes": 1000000, "a_budget": 100,
        "b_warn": 80, "b_budget": 100, "billing_cycle_day": 1, "billing_cycle_confirmed": True,
        "active_domain_id": "images.example.test",
        "domains": [
            {"id": host, "domain": host, "zone_id": "2" * 32, "enabled": True}
            for host in ("images.example.test", "backup.example.test")
        ],
    }


class SyntheticConfig:
    def __init__(self, settings: dict):
        self.settings = settings
        self.base_url = "https://app.example.test"
        self.cleanup_old_images = mock.Mock(return_value=0)

    def get_image_storage_settings(self):
        return deepcopy(self.settings)

    @property
    def images_dir(self):
        raise AssertionError("storage must not invoke a directory-creating property")

    @property
    def image_thumbnails_dir(self):
        raise AssertionError("storage must not create thumbnails")


class R2ImageStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        self.account = account_fixture()
        self.account_id = self.account["id"]
        self.settings = {
            "mode": "r2", "enabled": True, "r2_accounts": [self.account],
            "retention_minutes": 60, "usage_max_age_seconds": 900,
            "cache_ttl_seconds": 60, "public_base_url": "https://legacy.example.test",
        }
        self.config = SyntheticConfig(self.settings)
        for name, value in (("DATA_DIR", self.root), ("config", self.config)):
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
        self.objects: dict[str, bytes] = {}

        def upload(key, payload, **kwargs):
            self.objects[key] = payload
            return {"key": key, "etag": "synthetic-etag"}

        self.client.upload_bytes.side_effect = upload
        self.client.download_bytes.side_effect = lambda key: self.objects[key]
        self.client.delete_object.side_effect = lambda key: self.objects.pop(key, None)
        self.service = ImageStorageService(self.root / "image_index.json")
        self.protect(self.account)
        self.service.record_usage_snapshot(self.account_id, self.snapshot())
        self.now += timedelta(seconds=1)
        self.payload = image_bytes()

    def protect(self, account):
        self.service.update_r2_usage(account["id"], {
            "verified_domains": [item["domain"] for item in account["domains"]],
            "managed_disabled": True, "domains_checked": True, "domains_safe": True,
        })

    def snapshot(self, **updates):
        result = {
            "a_operations": 0, "b_operations": 0,
            "bucket_a_operations": 0, "bucket_b_operations": 0,
            "storage_bytes": 0, "bucket_storage_bytes": 0, "object_count": 0,
            "complete": True, "unknown_actions": [],
            "period_start": self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "period_end": self.now.isoformat(),
        }
        return {**result, **updates}

    def document(self):
        return json.loads(self.service.index_file.read_text(encoding="utf-8"))

    def assert_http(self, status, method, *args):
        with self.assertRaises(HTTPException) as caught:
            method(*args)
        self.assertEqual(caught.exception.status_code, status)

    def test_put_has_durable_pending_record_and_one_a_debit_before_network(self):
        original = self.client.upload_bytes.side_effect

        def inspect_put(key, payload, **kwargs):
            item = self.service.metadata(key)
            self.assertTrue(item["pending"])
            self.assertEqual(item["image_id"], key)
            self.assertEqual(item["object_key"], key)
            self.assertEqual(item["account_id"], self.account_id)
            self.assertFalse(item["local"])
            self.assertFalse(item["webdav"])
            self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)
            self.assertEqual(self.service.account_usage(self.account_id)["pending_bytes"], len(payload))
            self.assertEqual(self.service.list_items(""), [])
            self.assertFalse(self.service.exists(key))
            self.assert_http(404, self.service.public_url, key)
            self.assert_http(404, self.service.get_bytes, key)
            self.assertTrue(self.service.resolve_reference({"image_id": key})["expired"])
            return original(key, payload, **kwargs)

        self.client.upload_bytes.side_effect = inspect_put
        saved = self.service.save(self.payload)
        item = self.service.metadata(saved.rel)
        self.assertRegex(saved.rel, r"^images/2026/05/18/[0-9a-f]{32}\.png$")
        self.assertEqual(saved.image_id, saved.rel)
        self.assertEqual(saved.account_id, self.account_id)
        self.assertEqual(saved.expires_at, (self.now + timedelta(minutes=60)).isoformat())
        self.assertEqual(item["mime"], "image/png")
        self.assertEqual((item["width"], item["height"]), (3, 2))
        self.assertFalse(item["pending"])
        self.assertEqual(saved.url, f"https://images.example.test/{saved.rel}")
        self.assertEqual(self.client.upload_bytes.call_count, 1)
        self.assertEqual(self.client.upload_bytes.call_args.kwargs["cache_control"],
                         "public, max-age=0, s-maxage=60, must-revalidate")
        self.assertEqual(self.client.upload_bytes.call_args.kwargs["content_type"], "image/png")
        self.client.list_objects.assert_not_called()
        self.client.test_connection.assert_not_called()
        self.client.download_bytes.assert_not_called()
        self.client.close.assert_called_once()
        self.config.cleanup_old_images.assert_not_called()
        self.assertEqual({path.name for path in self.root.iterdir()}, {"image_index.json"})

    def test_jpeg_mime_extension_and_changed_retention_apply_only_to_new_images(self):
        first = self.service.save(self.payload)
        self.settings.update(retention_minutes=5, cache_ttl_seconds=45)
        second = self.service.save(image_bytes("JPEG"))
        self.assertTrue(second.rel.endswith(".jpg"))
        self.assertEqual(self.service.metadata(second.rel)["mime"], "image/jpeg")
        self.assertEqual(second.expires_at, (self.now + timedelta(minutes=5)).isoformat())
        self.assertEqual(self.service.metadata(first.rel)["expires_at"], first.expires_at)
        self.assertIn("s-maxage=45", self.client.upload_bytes.call_args.kwargs["cache_control"])

    def test_uncertain_put_keeps_budget_and_pending_until_known_key_delete(self):
        self.client.upload_bytes.side_effect = R2Error("synthetic sensitive transport details")
        with self.assertRaisesRegex(ImageStorageError, "^R2 image upload failed$"):
            self.service.save(self.payload)
        item = next(iter(self.document()["items"].values()))
        self.assertTrue(item["pending"])
        reopened = ImageStorageService(self.service.index_file)
        usage = reopened.account_usage(self.account_id)
        self.assertEqual((usage["a_local"], usage["pending_bytes"], usage["local_bytes"]), (1, len(self.payload), 0))
        self.assertEqual(self.client.upload_bytes.call_count, 1)
        self.assertNotIn("synthetic sensitive", self.service.index_file.read_text(encoding="utf-8"))
        self.assertEqual(reopened.cleanup_expired(), {"removed": 0, "failed": 0, "pending": 0})
        self.now += timedelta(minutes=61)
        self.account["enabled"] = False
        reopened.update_r2_usage(self.account_id, {"access_paused": True})
        self.assertEqual(reopened.cleanup_expired(), {"removed": 1, "failed": 0, "pending": 0})
        self.client.delete_object.assert_called_once_with(item["object_key"])
        self.assertEqual(reopened.account_usage(self.account_id)["pending_bytes"], 0)
        self.assertEqual(reopened.get_r2_usage(self.account_id)["a_local"], 1)

    def test_index_failure_before_put_propagates_without_network(self):
        before = self.service.index_file.read_bytes()
        with mock.patch.object(storage, "_write_json_object", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.service.save(self.payload)
        self.factory.assert_not_called()
        self.assertEqual(self.service.index_file.read_bytes(), before)

    def test_commit_failure_after_put_preserves_pending(self):
        write = storage._write_json_object

        def fail_commit(path, document):
            if any(not item["pending"] for item in document["items"].values()):
                raise OSError("synthetic commit failure")
            write(path, document)

        with mock.patch.object(storage, "_write_json_object", side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.service.save(self.payload)
        self.assertEqual(self.client.upload_bytes.call_count, 1)
        self.assertTrue(next(iter(self.document()["items"].values()))["pending"])
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)

    def test_corrupt_indexes_never_become_empty_successful_indexes(self):
        for raw in ("{broken", "[]", "{}", '{"items": []}', '{"items": {}, "r2_usage": []}'):
            with self.subTest(raw=raw):
                self.service.index_file.write_text(raw, encoding="utf-8")
                for operation in (lambda: self.service.save(self.payload),
                                  lambda: self.service.update_r2_usage(self.account_id, {"domains_safe": True}),
                                  lambda: self.service.list_items(""), self.service.cleanup_expired):
                    with self.assertRaises(ImageStorageError):
                        operation()
                    self.assertEqual(self.service.index_file.read_text(encoding="utf-8"), raw)
        self.factory.assert_not_called()

    def test_account_and_usage_results_are_copies(self):
        self.service.account(self.account_id)["domains"].clear()
        self.service.get_r2_usage(self.account_id)["verified_domains"].clear()
        self.assertEqual(len(self.service.account(self.account_id)["domains"]), 2)
        self.assertEqual(len(self.service.get_r2_usage(self.account_id)["verified_domains"]), 2)
        with self.assertRaises(ValueError):
            self.service.account("missing")

    def test_account_status_gates(self):
        original_document = self.service.index_file.read_bytes()
        cases = [
            ({}, {}, "eligible"),
            ({"enabled": False}, {}, "disabled"),
            ({"secret_access_key": ""}, {}, "unconfigured"),
            ({"billing_cycle_confirmed": False}, {}, "cycle_unconfirmed"),
            ({"active_domain_id": ""}, {}, "domains_unprotected"),
            ({}, {"managed_disabled": False}, "domains_unprotected"),
            ({}, {"domains_checked": False}, "domains_unprotected"),
            ({}, {"domains_safe": False}, "domains_unprotected"),
            ({}, {"verified_domains": ["images.example.test"]}, "domains_unprotected"),
            ({}, {"complete": False}, "usage_stale"),
            ({}, {"fetched_at": (self.now - timedelta(seconds=901)).isoformat()}, "usage_stale"),
            ({}, {"cloud_bytes": 1000000}, "capacity_exhausted"),
            ({}, {"a_local": 100}, "a_exhausted"),
            ({}, {"cloud_b": 100}, "b_exhausted"),
            ({"enabled": False}, {"access_paused": True}, "access_paused"),
        ]
        for account_updates, usage_updates, expected in cases:
            with self.subTest(expected=expected, account=account_updates, usage=usage_updates):
                self.account.clear()
                self.account.update(account_fixture())
                self.account.update(account_updates)
                self.service.index_file.write_bytes(original_document)
                self.service.update_r2_usage(self.account_id, usage_updates)
                self.assertEqual(self.service.account_status(self.account_id), expected)

    def test_no_eligible_account_never_falls_back_and_preflight_uses_actual_size(self):
        self.account["capacity_bytes"] = len(self.payload) - 1
        self.service.require_upload_available()
        with self.assertRaises(ImageStorageError):
            self.service.require_upload_available(len(self.payload))
        with self.assertRaises(ImageStorageError):
            self.service.save(self.payload)
        self.account["enabled"] = False
        with self.assertRaises(ImageStorageError):
            self.service.require_upload_available()
        self.factory.assert_not_called()
        self.assertFalse((self.root / "images").exists())
        self.settings["mode"] = "local"
        self.service.require_upload_available(len(self.payload))

    def test_random_choice_only_receives_eligible_accounts(self):
        second = deepcopy(self.account)
        second.update(id="00000000-0000-4000-8000-000000000002", account_id="3" * 32)
        self.settings["r2_accounts"].append(second)
        self.protect(second)
        self.service.record_usage_snapshot(second["id"], self.snapshot())
        self.account["capacity_bytes"] = len(self.payload) - 1
        with mock.patch.object(storage.random, "choice", side_effect=lambda candidates: candidates[0]) as choose:
            saved = self.service.save(self.payload)
        self.assertEqual([account["id"] for account in choose.call_args.args[0]], [second["id"]])
        self.assertEqual(saved.account_id, second["id"])
        self.assertEqual(self.factory.call_args.args[0]["account_id"], second["account_id"])
        self.assertEqual(self.factory.call_args.args[0]["prefix"], "images")

    def _assert_parallel_upload_gate(self):
        entered, release = Event(), Event()
        original = self.client.upload_bytes.side_effect

        def blocked_put(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release PUT")
            return original(*args, **kwargs)

        self.client.upload_bytes.side_effect = blocked_put
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.service.save, self.payload)
            try:
                self.assertTrue(entered.wait(5))
                # This completes while PUT is blocked: network does not own index lock.
                self.assertEqual(self.service.account_usage(self.account_id)["pending_bytes"], len(self.payload))
                second = executor.submit(self.service.save, self.payload)
                with self.assertRaises(ImageStorageError):
                    second.result(timeout=5)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(self.client.upload_bytes.call_count, 1)
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)

    def test_concurrent_capacity_reservations_cannot_spend_same_bytes(self):
        self.account["capacity_bytes"] = len(self.payload)
        self._assert_parallel_upload_gate()

    def test_concurrent_a_reservations_cannot_spend_same_operation(self):
        self.account["a_budget"] = 1
        self._assert_parallel_upload_gate()

    def test_atomic_management_a_increment_and_budget_gate(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda _: self.service.update_r2_usage(self.account_id, {"a_local_increment": 1}), range(12)))
        self.assertEqual(len(results), 12)
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 12)
        self.service.update_r2_usage(self.account_id, {"a_local": 2, "domains_safe": True})
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 12)
        self.account["a_budget"] = 12
        with self.assertRaises(ImageStorageError):
            self.service.update_r2_usage(self.account_id, {"a_local_increment": 1})
        self.assertNotIn("a_local_increment", self.service.get_r2_usage(self.account_id))

    def test_cloud_a_reconciles_by_max_without_losing_concurrent_put(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=10))
        original = self.client.upload_bytes.side_effect

        def refresh_during_put(*args, **kwargs):
            self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=10))
            self.service.update_r2_usage(self.account_id, {"a_local": 10})
            return original(*args, **kwargs)

        self.client.upload_bytes.side_effect = refresh_during_put
        self.service.save(self.payload)
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 11)
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=15))
        self.assertEqual((state["a_local"], state["cloud_a"]), (15, 15))
        self.assertEqual(self.service.account_usage(self.account_id)["effective_a"], 15)

    def test_query_preserves_local_operations_after_cutoff_when_cloud_count_grows(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=10))
        marker = self.service.get_r2_usage(self.account_id)
        cutoff = self.now.isoformat()
        self.now += timedelta(seconds=1)
        self.service.save(self.payload)
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot(
            a_operations=20, period_end=cutoff,
            a_query_cycle=marker.get("a_observed_cycle_start"),
            a_query_count=marker.get("a_observed_cycle_operations", 0),
        ))
        self.assertEqual(state["a_local"], 21)

    def test_replacing_empty_cf_account_does_not_inherit_old_account_a_budget(self):
        self.service.update_r2_usage(self.account_id, {"a_local_increment": 100})
        self.account["account_id"] = "3" * 32
        self.protect(self.account)
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot())
        self.assertEqual(state["a_local"], 0)
        self.assertEqual(self.service.account_status(self.account_id), "eligible")

    def test_bucket_observation_time_keeps_upload_delta_with_unindexed_old_objects(self):
        observed = self.now.isoformat()
        self.service.record_usage_snapshot(self.account_id, self.snapshot(
            storage_bytes=1000, bucket_storage_bytes=1000, bucket_storage_observed_at=observed))
        self.now += timedelta(seconds=1)
        self.service.save(self.payload)
        self.now += timedelta(seconds=1)
        self.service.record_usage_snapshot(self.account_id, self.snapshot(
            storage_bytes=1000, bucket_storage_bytes=1000, bucket_storage_observed_at=observed))
        self.assertEqual(self.service.account_usage(self.account_id)["effective_bytes"], 1000 + len(self.payload))

    def test_rollover_waits_for_complete_fresh_query_and_keeps_new_cycle_debits(self):
        self.service.update_r2_usage(self.account_id, {"a_local_increment": 4, "access_paused": True})
        old = self.service.get_r2_usage(self.account_id)
        self.now = datetime(2026, 6, 1, 0, 0, 1, tzinfo=timezone.utc)
        partial = self.service.record_usage_snapshot(self.account_id, self.snapshot(complete=False))
        self.assertEqual(partial["a_local"], 4)
        self.assertEqual(partial["fetched_at"], old["fetched_at"])
        self.service.update_r2_usage(self.account_id, {"a_local_increment": 1})
        fresh = self.service.record_usage_snapshot(self.account_id, self.snapshot())
        self.assertEqual(fresh["a_local"], 1)
        self.assertTrue(fresh["access_paused"])
        self.assertEqual(fresh["a_observed_cycle_operations"], 1)
        self.assertEqual(fresh["period_start"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(self.service.account_status(self.account_id), "access_paused")
        self.service.update_r2_usage(self.account_id, {"access_paused": False})
        self.assertEqual(self.service.account_status(self.account_id), "eligible")

    def test_cycle_anchor_is_utc_configured_day_not_startup_or_rolling_month(self):
        self.account["billing_cycle_day"] = 15
        self.now = datetime(2026, 6, 14, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(self.service.account_status(self.account_id), "usage_stale")
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot(period_start="2026-05-15T00:00:00Z"))
        self.assertTrue(state["complete"])
        self.assertEqual(state["period_start"], "2026-05-15T00:00:00+00:00")
        self.now = datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.service.account_status(self.account_id), "usage_stale")
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot(period_start="2026-06-15T00:00:00Z"))
        self.assertTrue(state["complete"])

    def test_snapshot_acceptance_does_not_extend_its_freshness_window(self):
        snapshot = self.snapshot()
        self.now += timedelta(seconds=800)
        state = self.service.record_usage_snapshot(self.account_id, snapshot)
        self.assertEqual(state["fetched_at"], snapshot["period_end"])
        self.assertEqual(self.service.account_status(self.account_id), "eligible")

        self.now += timedelta(seconds=100)
        self.assertEqual(self.service.account_status(self.account_id), "eligible")
        self.now += timedelta(seconds=1)
        self.assertEqual(self.service.account_status(self.account_id), "usage_stale")
        with self.assertRaises(ImageStorageError):
            self.service.require_upload_available()
        self.client.upload_bytes.assert_not_called()

    def test_incomplete_unknown_or_stale_snapshots_do_not_zero_previous_counts(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=20, storage_bytes=500))
        original = self.service.get_r2_usage(self.account_id)
        for changes in ({"complete": False}, {"unknown_actions": ["UnknownSyntheticAction"]},
                        {"period_end": (self.now - timedelta(seconds=901)).isoformat()},
                        {"storage_bytes": None}, {"a_operations": -1}):
            with self.subTest(changes=changes):
                result = self.service.record_usage_snapshot(self.account_id, self.snapshot(**changes))
                self.assertFalse(result["complete"])
                self.assertEqual(result["cloud_a"], 20)
                self.assertEqual(result["cloud_bytes"], 500)
                self.assertEqual(result["fetched_at"], original["fetched_at"])
                self.assertEqual(result["a_local"], 20)

    def test_out_of_order_complete_query_does_not_replace_newer_baseline(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(a_operations=20, storage_bytes=500))
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot(
            a_operations=10, storage_bytes=400, period_end=(self.now - timedelta(seconds=1)).isoformat()))
        self.assertEqual((state["cloud_a"], state["cloud_bytes"]), (20, 500))

    def test_capacity_baseline_plus_delta_does_not_double_count_confirmed_images(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(storage_bytes=1000))
        self.now += timedelta(seconds=1)
        saved = self.service.save(self.payload)
        self.assertEqual(self.service.account_usage(self.account_id)["effective_bytes"], 1000 + len(self.payload))
        self.now += timedelta(seconds=1)
        self.service.record_usage_snapshot(self.account_id, self.snapshot(
            storage_bytes=1000 + len(self.payload), bucket_storage_bytes=len(self.payload), object_count=1))
        usage = self.service.account_usage(self.account_id)
        self.assertEqual(usage["upload_delta_bytes"], 0)
        self.assertEqual(usage["effective_bytes"], 1000 + len(self.payload))
        self.service.delete(saved.rel)
        self.assertEqual(self.service.account_usage(self.account_id)["effective_bytes"], 1000 + len(self.payload))
        self.service.record_usage_snapshot(self.account_id, self.snapshot(storage_bytes=1000))
        self.assertEqual(self.service.account_usage(self.account_id)["effective_bytes"], 1000)

    def test_lagging_bucket_snapshot_cannot_hide_known_images_behind_other_buckets(self):
        self.service.record_usage_snapshot(self.account_id, self.snapshot(storage_bytes=1000))
        self.now += timedelta(seconds=1)
        self.service.save(self.payload)
        self.now += timedelta(seconds=1)
        # The statistics have not seen the dedicated image bucket yet.
        self.service.record_usage_snapshot(self.account_id, self.snapshot(storage_bytes=1000, bucket_storage_bytes=0))
        usage = self.service.account_usage(self.account_id)
        self.assertEqual(usage["upload_delta_bytes"], 0)
        self.assertEqual(usage["effective_bytes"], 1000 + len(self.payload))

    def test_pending_reservation_is_not_released_by_aggregate_snapshot(self):
        self.client.upload_bytes.side_effect = R2Error("synthetic uncertain PUT")
        with self.assertRaises(ImageStorageError):
            self.service.save(self.payload)
        self.now += timedelta(seconds=1)
        self.service.record_usage_snapshot(self.account_id, self.snapshot())
        usage = self.service.account_usage(self.account_id)
        self.assertEqual(usage["pending_bytes"], len(self.payload))
        self.assertEqual(usage["upload_delta_bytes"], len(self.payload))
        self.assertEqual(usage["effective_bytes"], len(self.payload))
        self.assertEqual(usage["a_local"], 1)

    def test_usage_query_cutoff_does_not_absorb_put_completing_after_cutoff(self):
        original = self.client.upload_bytes.side_effect

        def crossing_snapshot(*args, **kwargs):
            cutoff = self.now.isoformat()
            self.now += timedelta(seconds=1)
            self.service.record_usage_snapshot(self.account_id, self.snapshot(period_end=cutoff))
            self.assertEqual(self.service.account_usage(self.account_id)["pending_bytes"], len(self.payload))
            return original(*args, **kwargs)

        self.client.upload_bytes.side_effect = crossing_snapshot
        self.service.save(self.payload)
        self.assertEqual(self.service.account_usage(self.account_id)["upload_delta_bytes"], len(self.payload))

    def test_dynamic_url_and_reference_follow_active_domain(self):
        saved = self.service.save(self.payload)
        reference = {"image_id": saved.image_id, "account_id": saved.account_id, "expires_at": saved.expires_at}
        self.account["active_domain_id"] = "backup.example.test"
        self.account["domains"] = [domain for domain in self.account["domains"] if domain["id"] == "backup.example.test"]
        self.assertEqual(self.service.public_url(saved.rel), f"https://backup.example.test/{saved.rel}")
        self.assertEqual(self.service.resolve_reference(reference),
                         {**reference, "url": f"https://backup.example.test/{saved.rel}"})
        self.assertEqual(self.client.upload_bytes.call_count, 1)
        self.assertEqual(self.service.resolve_reference({"image_id": "images/missing.png"}),
                         {"image_id": "images/missing.png", "url": "", "expired": True})
        mismatched = {**reference, "account_id": "missing"}
        self.assertEqual(self.service.resolve_reference(mismatched), {**mismatched, "url": "", "expired": True})

    def test_expiry_metadata_and_paused_read_gate_survive_mode_or_domain_switch(self):
        saved = self.service.save(self.payload)
        self.service.update_r2_usage(self.account_id, {"access_paused": True})
        self.settings["mode"] = "local"
        self.account["active_domain_id"] = "backup.example.test"
        self.assert_http(403, self.service.public_url, saved.rel)
        self.assert_http(403, self.service.get_bytes, saved.rel)
        ref = self.service.resolve_reference({"image_id": saved.rel})
        self.assertEqual(ref["url"], "")
        self.assertTrue(ref["unavailable"])
        self.client.download_bytes.assert_not_called()
        self.assertEqual(self.service.list_items("")[0]["url"], "")
        self.service.update_r2_usage(self.account_id, {"access_paused": False})
        self.assertEqual(self.service.get_bytes(saved.rel), self.payload)
        self.now += timedelta(minutes=60)
        self.assert_http(410, self.service.public_url, saved.rel)
        self.assert_http(410, self.service.get_bytes, saved.rel)
        item = self.service.list_items("")[0]
        self.assertTrue(item["expired"])
        self.assertEqual(item["url"], "")
        self.assertFalse(self.service.exists(saved.rel))
        self.assertTrue(self.service.resolve_reference({"image_id": saved.rel})["expired"])
        self.assertFalse((self.root / "images").exists())

    def test_indexed_r2_get_delete_and_listing_survive_local_save_and_scan(self):
        saved = self.service.save(self.payload)
        self.settings.update(mode="local", public_base_url="")
        legacy = self.service.save(self.payload, "https://app.example.test")
        self.assertTrue((self.root / "images" / legacy.rel).is_file())
        listed = {item["path"]: item for item in self.service.list_items("https://app.example.test")}
        self.assertEqual(listed[saved.rel]["storage"], "r2")
        self.assertEqual(listed[legacy.rel]["storage"], "local")
        self.assertEqual(self.service.get_bytes(saved.rel), self.payload)
        self.assertFalse(self.service.has_local(saved.rel))
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)
        self.service.delete(legacy.rel)
        self.assertIsNotNone(self.service.metadata(saved.rel))
        self.assertTrue(self.service.delete(saved.rel))
        self.client.delete_object.assert_called_once_with(saved.rel)
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)

    def test_webdav_operations_preserve_r2_items_and_usage(self):
        saved = self.service.save(self.payload)
        self.settings.update(mode="webdav", public_base_url="https://legacy.example.test")
        with mock.patch.object(storage, "WebDAVClient") as webdav:
            webdav.return_value.put.return_value = "https://dav.example.test/synthetic.png"
            webdav.return_value.get.return_value = self.payload
            webdav.return_value.delete.return_value = True
            legacy = self.service.save(self.payload)
            self.assertEqual(legacy.storage, "webdav")
            self.assertEqual(self.service.get_bytes(legacy.rel), self.payload)
            self.assertEqual({item["storage"] for item in self.service.list_items("")}, {"r2", "webdav"})
            self.assertTrue(self.service.delete(legacy.rel))
        self.assertIsNotNone(self.service.metadata(saved.rel))
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 1)
        self.assertFalse((self.root / "images").exists())

    def test_pending_record_survives_legacy_scan_without_becoming_public(self):
        self.client.upload_bytes.side_effect = R2Error("synthetic failure")
        with self.assertRaises(ImageStorageError):
            self.service.save(self.payload)
        self.settings["mode"] = "local"
        self.assertEqual(self.service.list_items(""), [])
        self.assertEqual(len(self.document()["items"]), 1)
        self.assertEqual(self.service.account_usage(self.account_id)["pending_bytes"], len(self.payload))

    def test_cleanup_reuses_client_keeps_failed_index_and_reports_remaining_backlog(self):
        first = self.service.save(self.payload)
        second = self.service.save(self.payload)
        self.now += timedelta(minutes=61)
        self.factory.reset_mock()

        def delete(key):
            if key == first.rel:
                raise R2Error("synthetic private endpoint details")
            self.objects.pop(key, None)

        self.client.delete_object.side_effect = delete
        result = self.service.cleanup_expired()
        self.assertEqual(result, {"removed": 1, "failed": 1, "pending": 1})
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.client.close.call_count, 1)
        self.assertIsNone(self.service.metadata(second.rel))
        self.assertEqual(self.service.metadata(first.rel)["delete_error"], "R2 image delete failed")
        self.assertEqual(self.service.account_usage(self.account_id)["local_bytes"], len(self.payload))
        self.assertNotIn("synthetic private", self.service.index_file.read_text(encoding="utf-8"))
        self.assertEqual(self.client.delete_object.call_count, 2)
        self.client.delete_object.side_effect = lambda key: self.objects.pop(key, None)
        self.assertEqual(self.service.cleanup_expired(), {"removed": 1, "failed": 0, "pending": 0})
        self.assertEqual(self.service.get_r2_usage(self.account_id)["a_local"], 2)

    def test_cleanup_is_bounded_and_does_not_delete_not_yet_expired_images(self):
        self.service.save(self.payload)
        self.service.save(self.payload)
        self.now += timedelta(minutes=59)
        self.service.record_usage_snapshot(self.account_id, self.snapshot())
        self.service.save(self.payload)
        self.now += timedelta(minutes=2)
        with mock.patch.object(storage, "R2_CLEANUP_BATCH_SIZE", 1):
            self.assertEqual(self.service.cleanup_expired(), {"removed": 1, "failed": 0, "pending": 1})
        self.assertEqual(len(self.document()["items"]), 2)

    def test_delete_does_not_race_a_put_in_flight(self):
        original = self.client.upload_bytes.side_effect

        def inspect(key, payload, **kwargs):
            with self.assertRaises(ImageStorageError):
                self.service.delete(key)
            self.assertTrue(self.service.metadata(key)["pending"])
            return original(key, payload, **kwargs)

        self.client.upload_bytes.side_effect = inspect
        saved = self.service.save(self.payload)
        self.client.delete_object.assert_not_called()
        self.assertTrue(self.service.delete(saved.rel))

    def test_successful_remote_delete_with_index_failure_does_not_release_index(self):
        saved = self.service.save(self.payload)
        with mock.patch.object(storage, "_write_json_object", side_effect=OSError("synthetic index failure")):
            with self.assertRaises(OSError):
                self.service.delete(saved.rel)
        self.assertIsNotNone(self.service.metadata(saved.rel))
        # A subsequent known-key delete of an already missing object releases it.
        self.assertTrue(self.service.delete(saved.rel))
        self.assertIsNone(self.service.metadata(saved.rel))

    def test_empty_account_identity_change_requires_new_protection_and_usage(self):
        self.account.update(account_id="9" * 32, bucket="different-synthetic-bucket")
        self.assertEqual(self.service.account_status(self.account_id), "domains_unprotected")
        self.protect(self.account)
        self.assertEqual(self.service.account_status(self.account_id), "usage_stale")
        state = self.service.record_usage_snapshot(self.account_id, self.snapshot())
        self.assertEqual(state["usage_cf_account_id"], "9" * 32)
        self.assertEqual(state["usage_bucket"], "different-synthetic-bucket")
        self.assertEqual(self.service.account_status(self.account_id), "eligible")

    def test_settings_identity_guard_includes_pending_and_allows_rotation(self):
        self.client.upload_bytes.side_effect = R2Error("synthetic failure")
        with self.assertRaises(ImageStorageError):
            self.service.save(self.payload)
        original = deepcopy(self.settings)
        for change in ({"account_id": "9" * 32}, {"bucket": "different-bucket"}, {"secret_access_key": ""}):
            candidate = deepcopy(original)
            candidate["r2_accounts"][0].update(change)
            with self.assertRaises(ValueError):
                self.service.validate_settings_change(candidate)
        with self.assertRaises(ValueError):
            self.service.validate_settings_change({**original, "r2_accounts": []})
        candidate = deepcopy(original)
        candidate["mode"] = "local"
        candidate["r2_accounts"][0].update(enabled=False, secret_access_key="synthetic-rotated-secret",
                                            active_domain_id="backup.example.test")
        self.service.validate_settings_change(candidate)
        self.service.delete(next(iter(self.document()["items"])))
        self.service.validate_settings_change({**original, "r2_accounts": []})


if __name__ == "__main__":
    unittest.main()
