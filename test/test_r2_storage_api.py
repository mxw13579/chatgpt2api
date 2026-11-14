"""Router/authentication tests using only synthetic identities and service mocks."""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.image_storage as module
from services.cloudflare_api import CloudflareAPIError
from services.r2_management_service import R2ManagementService


ACCOUNT = "00000000-0000-4000-8000-000000000001"
BASE = "/api/image-storage"
ACCOUNT_PATH = BASE + "/accounts/" + ACCOUNT
ADMIN = {"Authorization": "Bearer synthetic-admin"}
MEMBER = {"Authorization": "Bearer synthetic-member"}


def identity(authorization):
    if authorization == ADMIN["Authorization"]:
        return {"id": "synthetic-admin", "role": "admin"}
    if authorization == MEMBER["Authorization"]:
        return {"id": "synthetic-member", "role": "user"}
    raise HTTPException(status_code=401, detail={"error": "unauthorized"})


def admin(authorization):
    result = identity(authorization)
    if result["role"] != "admin":
        raise HTTPException(status_code=403, detail={"error": "admin required"})
    return result


class R2StorageApiTests(unittest.TestCase):
    def setUp(self):
        self.service = mock.Mock(spec=R2ManagementService)
        self.service.overview.return_value = {"settings": {"r2_accounts": []}, "accounts": []}
        self.service.save_settings.return_value = self.service.overview.return_value
        self.service.resolve_images.return_value = [{"image_id": "images/fixture.png", "url": "", "expired": True}]
        self.service.test_connection.return_value = {"ok": True}
        self.service.refresh_usage.return_value = {"complete": True, "cloud_a": 1, "cloud_b": 2}
        self.service.list_zones.return_value = [{"id": "b" * 32, "name": "example.test"}]
        self.service.list_domains.return_value = {"items": [], "managed_enabled": False}
        self.service.add_domain.return_value = {"id": "new.example.test", "enabled": False}
        self.service.update_domain.return_value = {"id": "new.example.test", "enabled": True}
        self.service.check_domains.return_value = [{"domain_id": "new.example.test", "ok": False, "source": "server"}]
        self.service.apply_protection.return_value = {"ok": True}
        self.service.restore_access.return_value = {"ok": True}
        self.service.cleanup.return_value = {"removed": 1, "failed": 0, "pending": 0}
        self.service.cleanup_legacy.return_value = {"removed": 1}
        for patcher in (
            mock.patch.object(module, "r2_management_service", self.service),
            mock.patch.object(module, "require_identity", side_effect=identity),
            mock.patch.object(module, "require_admin", side_effect=admin),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(module.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_all_management_routes_require_admin(self):
        cases = [
            ("GET", BASE, None),
            ("PUT", BASE, {"mode": "r2", "enabled": True}),
            ("POST", ACCOUNT_PATH + "/test", None),
            ("POST", ACCOUNT_PATH + "/usage/refresh", None),
            ("GET", ACCOUNT_PATH + "/zones", None),
            ("GET", ACCOUNT_PATH + "/domains", None),
            ("POST", ACCOUNT_PATH + "/domains", {"domain": "new.example.test", "zone_id": "b" * 32}),
            ("PATCH", ACCOUNT_PATH + "/domains/new.example.test", {"active": True}),
            ("DELETE", ACCOUNT_PATH + "/domains/new.example.test", None),
            ("POST", ACCOUNT_PATH + "/domains/check", {}),
            ("PUT", ACCOUNT_PATH + "/protection", {"rate_limit_requests": 120, "restrict_methods": True, "cors_origins": ["*"]}),
            ("POST", ACCOUNT_PATH + "/access/restore", None),
            ("POST", BASE + "/cleanup", None),
            ("POST", BASE + "/legacy/cleanup", {"confirm": True}),
        ]
        for method, path, body in cases:
            for headers, expected in ((MEMBER, 403), ({}, 401)):
                with self.subTest(method=method, path=path, status=expected):
                    response = self.client.request(method, path, headers=headers, json=body)
                    self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.service.mock_calls, [])

    def test_resolve_accepts_identity_without_admin_and_rejects_anonymous(self):
        body = {"images": [{"image_id": "images/fixture.png", "account_id": ACCOUNT, "url": "https://old.example.test/ignored"}]}
        response = self.client.post(BASE + "/resolve", headers=MEMBER, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["url"], "")
        self.service.resolve_images.assert_called_once_with([{"image_id": "images/fixture.png", "account_id": ACCOUNT}])
        self.assertEqual(self.client.post(BASE + "/resolve", json=body).status_code, 401)

    def test_overview_and_full_settings_contract(self):
        response = self.client.get(BASE, headers=ADMIN)
        self.assertEqual(response.json(), {"settings": {"r2_accounts": []}, "accounts": []})
        settings = {"enabled": True, "mode": "r2", "retention_minutes": 60, "r2_accounts": []}
        response = self.client.put(BASE, headers=ADMIN, json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        self.service.save_settings.assert_called_once_with(settings)

    def test_domain_routes_and_manual_switch_actor(self):
        response = self.client.get(ACCOUNT_PATH + "/domains", headers=ADMIN)
        self.assertEqual(response.json(), {"items": [], "managed_enabled": False})
        response = self.client.get(ACCOUNT_PATH + "/zones", headers=ADMIN)
        self.assertEqual(response.json()["items"][0]["name"], "example.test")
        response = self.client.post(ACCOUNT_PATH + "/domains", headers=ADMIN,
                                    json={"domain": "new.example.test", "zone_id": "b" * 32})
        self.assertFalse(response.json()["item"]["enabled"])
        response = self.client.patch(ACCOUNT_PATH + "/domains/new.example.test", headers=ADMIN, json={"active": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.service.update_domain.assert_called_once_with(ACCOUNT, "new.example.test", {"active": True}, "synthetic-admin")
        response = self.client.post(ACCOUNT_PATH + "/domains/check", headers=ADMIN, json={"domain_id": "new.example.test"})
        self.assertEqual(response.json()["items"][0]["source"], "server")
        response = self.client.delete(ACCOUNT_PATH + "/domains/new.example.test", headers=ADMIN)
        self.assertEqual(response.json(), {"ok": True})
        self.service.delete_domain.assert_called_once_with(ACCOUNT, "new.example.test")

    def test_usage_test_restore_and_cleanup_result_envelopes(self):
        for path, expected in (
            (ACCOUNT_PATH + "/test", {"ok": True}),
            (ACCOUNT_PATH + "/usage/refresh", {"complete": True, "cloud_a": 1, "cloud_b": 2}),
            (ACCOUNT_PATH + "/access/restore", {"ok": True}),
            (BASE + "/cleanup", {"removed": 1, "failed": 0, "pending": 0}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, headers=ADMIN)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"result": expected})

    def test_protection_forwards_free_template_inputs(self):
        response = self.client.put(ACCOUNT_PATH + "/protection", headers=ADMIN,
                                   json={"rate_limit_requests": 120, "restrict_methods": False, "cors_origins": ["https://app.example.test"]})
        self.assertEqual(response.json(), {"result": {"ok": True}})
        self.service.apply_protection.assert_called_once_with(ACCOUNT, 120, False, ["https://app.example.test"])

    def test_legacy_cleanup_requires_explicit_confirmation_and_not_r2_cleanup(self):
        for body in ({}, {"confirm": False}):
            response = self.client.post(BASE + "/legacy/cleanup", headers=ADMIN, json=body)
            self.assertEqual(response.status_code, 400, response.text)
        self.service.cleanup_legacy.assert_not_called()
        response = self.client.post(BASE + "/legacy/cleanup", headers=ADMIN, json={"confirm": True})
        self.assertEqual(response.json(), {"result": {"removed": 1}})
        self.service.cleanup_legacy.assert_called_once_with()
        self.service.cleanup.assert_not_called()

    def test_cloud_and_config_errors_are_sanitized(self):
        self.service.refresh_usage.side_effect = CloudflareAPIError("synthetic-private-url-token", status_code=403, codes=(10000,))
        response = self.client.post(ACCOUNT_PATH + "/usage/refresh", headers=ADMIN)
        self.assertEqual(response.status_code, 502)
        self.assertIn("403", response.text)
        self.assertNotIn("synthetic-private", response.text)
        self.service.save_settings.side_effect = ValueError("synthetic-private-config-token")
        response = self.client.put(BASE, headers=ADMIN, json={"mode": "r2"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic-private", response.text)

    def test_app_registers_router_and_only_skips_old_startup_cleanup_in_r2(self):
        from api import app as app_module
        for mode in ("r2", "local", "webdav"):
            with self.subTest(mode=mode), \
                    mock.patch.object(app_module, "config") as config, \
                    mock.patch.object(app_module, "start_limited_account_watcher") as watcher, \
                    mock.patch.object(app_module, "start_image_cleanup_scheduler") as scheduler, \
                    mock.patch.object(app_module, "backup_service") as backup:
                config.app_version = "synthetic-test"
                config.get_image_storage_settings.return_value = {"mode": mode}
                app = app_module.create_app()
                with TestClient(app) as client:
                    response = client.get(BASE, headers=ADMIN)
                    self.assertEqual(response.status_code, 200, response.text)
                scheduler.assert_called_once()
                scheduler.return_value.join.assert_called_once_with(timeout=1)
                watcher.return_value.join.assert_called_once_with(timeout=1)
                backup.start.assert_called_once()
                backup.stop.assert_called_once()
                if mode == "r2":
                    config.cleanup_old_images.assert_not_called()
                else:
                    config.cleanup_old_images.assert_called_once()


if __name__ == "__main__":
    unittest.main()
