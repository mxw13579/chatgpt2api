"""Synthetic snapshot/read-count regressions; main runs these in the isolated runner.

No real storage endpoints, credentials, configuration or task data are used.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services import image_service
from services import image_storage_service as storage
from test.test_r2_image_storage import SyntheticConfig, account_fixture, image_bytes


class R2IndexReadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        self.account = account_fixture()
        self.account_id = self.account["id"]
        self.settings = {
            "mode": "local", "r2_accounts": [self.account], "public_base_url": "",
            "retention_minutes": 60, "usage_max_age_seconds": 900, "cache_ttl_seconds": 60,
        }
        self.config = SyntheticConfig(self.settings)
        self.service = storage.ImageStorageService(self.root / "image_index.json")
        self.payload = image_bytes()
        for module, name, value in (
            (storage, "DATA_DIR", self.root), (storage, "config", self.config),
            (image_service, "DATA_DIR", self.root), (image_service, "config", self.config),
            (image_service, "image_storage_service", self.service),
        ):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.object(storage, "_utc_now", return_value=self.now),
            mock.patch.object(image_service, "load_tags", return_value={}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service.update_r2_usage(self.account_id, {
            "verified_domains": [domain["domain"] for domain in self.account["domains"]],
            "managed_disabled": True, "domains_checked": True, "domains_safe": True,
            "domains": [{"domain": "images.example.test", "ready": True}],
        })
        self.service.record_usage_snapshot(self.account_id, self.snapshot())

    def snapshot(self):
        return {
            "a_operations": 0, "b_operations": 0,
            "bucket_a_operations": 0, "bucket_b_operations": 0,
            "storage_bytes": 0, "bucket_storage_bytes": 0, "object_count": 0,
            "complete": True, "unknown_actions": [],
            "period_start": self.now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "period_end": self.now.isoformat(),
        }

    def document(self):
        return json.loads(self.service.index_file.read_text(encoding="utf-8"))

    def write_document(self, document):
        self.service.index_file.write_text(json.dumps(document), encoding="utf-8")

    def r2_item(self, name="synthetic.png", *, pending=False, size=20):
        rel = f"images/2026/05/18/{name}"
        return {
            "rel": rel, "path": rel, "image_id": rel, "object_key": rel,
            "storage": "r2", "account_id": self.account_id,
            "size": size, "date": "2026-05-18", "created_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(hours=1)).isoformat(),
            "pending": pending, "local": False, "webdav": False,
        }

    def local_file(self, rel):
        path = self.root / "images" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_account_summaries_share_one_document_settings_and_usage_view(self):
        second = deepcopy(self.account)
        second.update(id="00000000-0000-4000-8000-000000000002", enabled=False)
        self.settings["r2_accounts"].append(second)
        self.account["capacity_bytes"] = 40
        document = self.document()
        for item in (self.r2_item(size=29), self.r2_item("pending.png", pending=True, size=11)):
            document["items"][item["rel"]] = item
        document["r2_usage"][second["id"]] = {"access_paused": True, "domains": []}
        self.write_document(document)

        with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings, \
                mock.patch.object(self.service, "_usage_view", wraps=self.service._usage_view) as usage, \
                mock.patch.object(self.service, "_account_status", wraps=self.service._account_status) as status:
            summaries = self.service.account_summaries()
        load.assert_called_once_with()
        settings.assert_called_once_with()
        self.assertEqual(usage.call_count, 2)
        self.assertIs(usage.call_args_list[0].args[0], usage.call_args_list[1].args[0])
        self.assertEqual([item["status"] for item in summaries], ["capacity_exhausted", "access_paused"])
        self.assertEqual(summaries[0]["usage"]["pending_bytes"], 11)
        self.assertEqual(summaries[0]["usage"]["effective_bytes"], 40)
        for call, summary in zip(status.call_args_list, summaries):
            self.assertEqual(set(summary), {"id", "status", "usage", "domains"})
            self.assertIs(call.args[1], summary["usage"])
            self.assertEqual(summary["domains"], summary["usage"].get("domains", []))
        summaries[0]["domains"].clear()
        self.assertTrue(self.document()["r2_usage"][self.account_id]["domains"])
        self.service.update_r2_usage(self.account_id, {"access_paused": True})
        self.assertEqual(self.service.account_summaries()[0]["status"], "access_paused")

    def test_individual_status_and_usage_snapshot_read_settings_once(self):
        for operation in (
            lambda: self.service.account_status(self.account_id),
            lambda: self.service.record_usage_snapshot(self.account_id, self.snapshot()),
        ):
            with self.subTest(operation=operation), \
                    mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                    mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings:
                operation()
                load.assert_called_once_with()
                settings.assert_called_once_with()
        self.assertEqual(self.service.account_status(self.account_id), "eligible")

    def test_three_legacy_images_list_without_per_thumbnail_metadata_reads(self):
        paths = [f"2026/05/18/legacy-{number}.png" for number in range(3)]
        for rel in paths:
            self.local_file(rel)
        usage = self.document()["r2_usage"]
        with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "metadata", side_effect=AssertionError("no thumbnail lookup")), \
                mock.patch.object(storage, "_write_json_object", wraps=storage._write_json_object) as write:
            result = image_service.list_images("https://app.example.test")
        load.assert_called_once_with()
        write.assert_called_once()
        self.assertEqual({item["path"] for item in result["items"]}, set(paths))
        for item in result["items"]:
            self.assertEqual(item["thumbnail_url"], f"https://app.example.test/image-thumbnails/{item['path']}")
            self.assertEqual((item["width"], item["height"]), (3, 2))
        self.assertEqual(self.document()["r2_usage"], usage)

    def test_legacy_mutations_and_listing_use_one_snapshot_in_all_modes(self):
        r2 = self.r2_item()
        document = self.document()
        document["items"][r2["rel"]] = r2
        self.write_document(document)
        usage = document["r2_usage"]
        with mock.patch.object(storage, "WebDAVClient") as webdav:
            webdav.return_value.put.return_value = "https://dav.example.test/synthetic.png"
            webdav.return_value.delete.return_value = True
            for mode in ("local", "webdav", "both"):
                with self.subTest(mode=mode):
                    self.settings.update(mode=mode, public_base_url="https://legacy.example.test")
                    rel = f"2026/05/18/{mode}.png"
                    with mock.patch.object(self.service, "make_relative_path", return_value=rel), \
                            mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                            mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings:
                        stored = self.service.save(self.payload)
                    load.assert_called_once_with()
                    settings.assert_called_once_with()
                    self.assertEqual(stored.storage, mode)
                    self.assertEqual(stored.url, f"https://legacy.example.test/{rel}")
                    self.assertEqual(self.document()["r2_usage"], usage)

                    self.settings["public_base_url"] = "https://changed.example.test"
                    self.account["active_domain_id"] = "backup.example.test"
                    with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                            mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings:
                        listed = {item["path"]: item for item in self.service.list_items("")}
                    load.assert_called_once_with()
                    settings.assert_called_once_with()
                    self.assertEqual(listed[rel]["url"], f"https://changed.example.test/{rel}")
                    self.assertEqual(listed[r2["rel"]]["url"], f"https://backup.example.test/{r2['rel']}")
                    with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                            mock.patch.object(storage, "_write_json_object", wraps=storage._write_json_object) as write:
                        self.assertTrue(self.service.delete(rel))
                    load.assert_called_once_with()
                    write.assert_called_once()
                    self.assertEqual(self.document(), {"items": {r2["rel"]: r2}, "r2_usage": usage})

    def test_r2_listing_reuses_one_account_mapping_but_refreshes_next_operation(self):
        self.settings["mode"] = "r2"
        document = self.document()
        for number in range(3):
            item = self.r2_item(f"{number}.png")
            document["items"][item["rel"]] = item
        self.write_document(document)
        for host in ("images.example.test", "backup.example.test"):
            self.account["active_domain_id"] = host
            with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                    mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings, \
                    mock.patch.object(self.service, "account", side_effect=AssertionError("no per-image account lookup")):
                listed = self.service.list_items("")
            load.assert_called_once_with()
            settings.assert_called_once_with()
            self.assertEqual(len(listed), 3)
            self.assertTrue(all(item["url"] == f"https://{host}/{item['path']}" for item in listed))
        self.assertEqual(self.document(), document)
        self.assertFalse((self.root / "images").exists())

    def test_r2_put_reloads_after_network_and_reuses_commit_for_url(self):
        self.settings["mode"] = "r2"

        def upload(key, payload, **kwargs):
            document = self.document()
            self.assertTrue(document["items"][key]["pending"])
            document["r2_usage"][self.account_id]["management_marker"] = "synthetic concurrent update"
            document["items"]["legacy.png"] = {"storage": "webdav", "webdav": True}
            self.write_document(document)
            self.account["active_domain_id"] = "backup.example.test"

        with mock.patch.object(storage, "CloudflareR2Client") as factory, \
                mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings, \
                mock.patch.object(self.service, "public_url", side_effect=AssertionError("reuse commit snapshot")):
            factory.return_value.upload_bytes.side_effect = upload
            saved = self.service.save(self.payload)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(settings.call_count, 2)
        self.assertEqual(saved.url, f"https://backup.example.test/{saved.rel}")
        document = self.document()
        self.assertFalse(document["items"][saved.rel]["pending"])
        self.assertIn("legacy.png", document["items"])
        self.assertEqual(document["r2_usage"][self.account_id]["management_marker"], "synthetic concurrent update")
        self.assertEqual(document["r2_usage"][self.account_id]["a_local"], 1)
        self.assertFalse((self.root / "images").exists())

    def test_r2_delete_reuses_selection_and_reloads_after_network(self):
        self.settings["mode"] = "r2"
        item = self.r2_item()
        document = self.document()
        document["items"][item["rel"]] = item
        self.write_document(document)

        def remote_delete(key):
            self.assertEqual(key, item["object_key"])
            document = self.document()
            document["r2_usage"][self.account_id]["management_marker"] = "synthetic concurrent update"
            document["items"]["legacy.png"] = {"storage": "webdav", "webdav": True}
            self.write_document(document)

        with mock.patch.object(storage, "CloudflareR2Client") as factory, \
                mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings:
            factory.return_value.delete_object.side_effect = remote_delete
            self.assertTrue(self.service.delete(item["rel"]))
        self.assertEqual(load.call_count, 2)
        settings.assert_called_once_with()
        factory.return_value.close.assert_called_once_with()
        document = self.document()
        self.assertEqual(set(document["items"]), {"legacy.png"})
        self.assertEqual(document["r2_usage"][self.account_id]["management_marker"], "synthetic concurrent update")

    def test_webdav_sync_saves_the_loaded_document_once_and_keeps_r2(self):
        self.settings["mode"] = "both"
        document = self.document()
        r2 = self.r2_item(pending=True)
        document["items"][r2["rel"]] = r2
        self.write_document(document)
        self.local_file(r2["rel"])
        for number in range(3):
            self.local_file(f"2026/05/18/legacy-{number}.png")
        with mock.patch.object(storage, "WebDAVClient") as webdav, \
                mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "settings", wraps=self.service.settings) as settings, \
                mock.patch.object(storage, "_write_json_object", wraps=storage._write_json_object) as write:
            webdav.return_value.put.return_value = "https://dav.example.test/synthetic.png"
            self.assertEqual(self.service.sync_all(), {"uploaded": 3, "skipped": 1, "failed": 0})
        load.assert_called_once_with()
        settings.assert_called_once_with()
        write.assert_called_once()
        self.assertEqual(webdav.return_value.put.call_count, 3)
        updated = self.document()
        self.assertEqual(updated["r2_usage"], document["r2_usage"])
        self.assertEqual(updated["items"][r2["rel"]], r2)

    def test_explicit_legacy_cleanup_unlinks_then_reconciles_once(self):
        from services.image_task_service import image_task_service

        self.settings["mode"] = "r2"
        document = self.document()
        r2 = self.r2_item(pending=True)
        document["items"] = {
            "local-1.png": {"storage": "local", "local": True, "date": "2026-05-18"},
            "local-2.png": {"storage": "local", "local": True, "date": "2026-05-18"},
            "both.png": {"storage": "both", "local": True, "webdav": True, "date": "2026-05-18"},
            "remote.png": {"storage": "webdav", "local": False, "webdav": True, "date": "2026-05-18"},
            r2["rel"]: r2,
        }
        self.write_document(document)
        originals = [self.local_file(rel) for rel in ("local-1.png", "local-2.png", "both.png", "raw.txt", r2["rel"])]
        thumbnails = self.root / "image_thumbnails"
        thumbnails.mkdir()
        for name in ("first.png", "second.png"):
            (thumbnails / name).write_bytes(self.payload)
        # Ordinary R2 listing never implicitly removes these legacy files.
        image_service.list_images("https://app.example.test")
        self.assertTrue(all(path.is_file() for path in originals))
        self.config.cleanup_old_images.assert_not_called()
        with mock.patch.object(self.service, "_load_document", wraps=self.service._load_document) as load, \
                mock.patch.object(self.service, "list_items", wraps=self.service.list_items) as reconcile, \
                mock.patch.object(self.service, "metadata", side_effect=AssertionError("use one selection snapshot")), \
                mock.patch.object(self.service, "delete", side_effect=AssertionError("no per-file delete")), \
                mock.patch.object(storage, "_write_json_object", wraps=storage._write_json_object) as write, \
                mock.patch.object(storage, "WebDAVClient") as webdav, \
                mock.patch.object(storage, "CloudflareR2Client") as r2_client, \
                mock.patch.object(image_service, "remove_tags") as tags, \
                mock.patch.object(image_task_service, "clear_legacy_image_payloads",
                                  return_value={"tasks": 1, "payloads": 2}) as clear:
            result = image_service.cleanup_legacy_images()
        self.assertEqual(result, {"removed": 4, "thumbnails": 2, "tasks": 1, "payloads": 2})
        # One selection snapshot, then one fresh reconciliation after local I/O.
        self.assertEqual(load.call_count, 2)
        reconcile.assert_called_once_with("")
        write.assert_called_once()
        webdav.assert_not_called()
        r2_client.assert_not_called()
        clear.assert_called_once_with()
        self.assertEqual({call.args[0] for call in tags.call_args_list}, {"local-1.png", "local-2.png", "raw.txt"})
        self.assertTrue(originals[-1].is_file())
        self.assertTrue(all(not path.exists() for path in originals[:-1]))
        self.assertEqual(list(thumbnails.rglob("*")), [])
        updated = self.document()
        self.assertEqual(set(updated["items"]), {"both.png", "remote.png", r2["rel"]})
        self.assertEqual(updated["items"]["both.png"]["storage"], "webdav")
        self.assertFalse(updated["items"]["both.png"]["local"])
        self.assertEqual(updated["items"][r2["rel"]], r2)
        self.assertEqual(updated["r2_usage"], document["r2_usage"])


if __name__ == "__main__":
    unittest.main()
