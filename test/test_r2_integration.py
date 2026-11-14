"""Synthetic integration tests; run ONLY in test/run_isolated.py's source copy.

ConfigStore, image/index storage, management, task references, audit logs and
FastAPI routers are real. Only the Cloudflare control/object clients are fakes;
paths and clocks are redirected to fixtures. No app lifespan, generation,
background maintenance, public HEAD, or real authentication/cloud call runs.
These fixtures do not establish real CF permissions, billing or CDN behaviour.
"""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import image_storage as storage_api, image_tasks as tasks_api, support, system
from services import config as config_module
from services import image_service, image_storage_service as storage_module
from services import image_tags_service, image_task_service as task_module
from services import r2_management_service as management
from services.cloudflare_api import CloudflareAPIError
from services.log_service import LogService
from services.protocol import conversation
from services.r2_client import R2Error
from test.test_r2_image_storage import image_bytes
from test.test_r2_management import ACCOUNT, HOSTS, NOW, ZONE, FakeCloudflare, settings_fixture

BASE = "/api/image-storage"
ACCOUNT_PATH = f"{BASE}/accounts/{ACCOUNT}"
AUTH = "synthetic-integration-admin"
SECRET_FIELDS = ("access_key_id", "secret_access_key", "api_token")
SECRET_VALUES = tuple(settings_fixture()["r2_accounts"][0][field] for field in SECRET_FIELDS)
TASK_ID = "synthetic-r2-task"
PROTECTION = {"rate_limit_requests": 120, "restrict_methods": True, "cors_origins": ["https://app.example.test"]}


def assert_redacted(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Do not print even the synthetic sentinel values on a failing assertion.
    assert all(secret not in text for secret in (*SECRET_VALUES, "synthetic-private")), "R2 secret leaked"


class IntegrationCloudflare(FakeCloudflare):
    """Reuse the control-plane fake, but observe the REAL persisted pause gate."""

    def __init__(self, storage):
        super().__init__(storage)
        self.domains.clear()  # Onboarding starts with no bucket bindings.
        self.managed = True

    def update_domain(self, bucket, domain, enabled):
        self.calls.append(("domain", domain, enabled))
        if not enabled:
            self.gates_at_disable.append(bool(self.storage.get_r2_usage(ACCOUNT).get("access_paused")))
        if domain in (self.enable_failures if enabled else self.disable_failures):
            raise CloudflareAPIError("synthetic-private-transport", status_code=403, codes=(10000,))
        self.domains[domain]["enabled"] = enabled
        return copy.deepcopy(self.domains[domain])


class ObjectClient:
    """In-memory CF object transport, keyed by bucket as well as object key."""

    def __init__(self, settings, remote):
        assert settings["prefix"] == "images"
        self.bucket = settings["bucket"]
        self.remote = remote

    def upload_bytes(self, key, payload, **headers):
        self.remote.calls.append(("PUT", self.bucket, key))
        self.remote.uploads.append(copy.deepcopy(headers))
        if self.remote.on_put is not None:
            self.remote.on_put(key)
        self.remote.objects[self.bucket, key] = bytes(payload)
        if self.remote.upload_error is not None:
            # An uncertain PUT may have reached CF despite the client error.
            raise self.remote.upload_error
        return {"key": key, "etag": "synthetic-etag"}

    def download_bytes(self, key):
        self.remote.calls.append(("GET", self.bucket, key))
        return self.remote.objects[self.bucket, key]

    def delete_object(self, key):
        self.remote.calls.append(("DELETE", self.bucket, key))
        self.remote.objects.pop((self.bucket, key), None)

    def close(self):
        pass


@pytest.fixture
def integrated(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CHATGPT2API_AUTH_KEY", AUTH)
    monkeypatch.setattr(config_module, "DATA_DIR", data_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    store = config_module.ConfigStore(config_path)
    storage = storage_module.ImageStorageService(data_dir / "image_index.json")
    manager = management.R2ManagementService()
    logs = LogService(data_dir / "logs.jsonl")
    clock = [NOW]

    # These are real service instances, including the singleton imported lazily
    # by ConfigStore.update for its outstanding-object/atomic reservation guard.
    for module in (config_module, storage_module, management, image_service, task_module,
                   support, system, tasks_api):
        monkeypatch.setattr(module, "config", store)
    for module in (storage_module, management, image_service, task_module, system, conversation):
        monkeypatch.setattr(module, "image_storage_service", storage)
    for module in (storage_module, image_service):
        monkeypatch.setattr(module, "DATA_DIR", data_dir)
    for module in (management, task_module, system):
        monkeypatch.setattr(module, "log_service", logs)
    monkeypatch.setattr(image_tags_service, "TAGS_FILE", data_dir / "image_tags.json")
    monkeypatch.setattr(storage_module, "_utc_now", lambda: clock[0])
    monkeypatch.setattr(management, "_now", lambda: clock[0])
    monkeypatch.setattr(storage_api, "r2_management_service", manager)
    monkeypatch.setattr(system, "r2_management_service", manager)

    cloud = IntegrationCloudflare(storage)
    remote = SimpleNamespace(objects={}, calls=[], uploads=[], on_put=None, upload_error=None)
    monkeypatch.setattr(management, "CloudflareAPI", lambda account_id, api_token: cloud)
    for module in (storage_module, management):
        monkeypatch.setattr(module, "CloudflareR2Client", lambda settings: ObjectClient(settings, remote))

    # Seed only task bookkeeping. Tests pass a storage receipt through the real
    # formatter/completion/persistence path; no upstream generator is called.
    task_path = data_dir / "image_tasks.json"
    timestamp = task_module._now_iso()
    task_path.write_text(json.dumps({"tasks": [{
        "id": TASK_ID, "owner_id": "admin", "status": "success", "mode": "generate",
        "created_at": timestamp, "updated_at": timestamp, "data": [],
    }]}), encoding="utf-8")
    tasks = task_module.ImageTaskService(task_path)
    monkeypatch.setattr(tasks_api, "image_task_service", tasks)

    app = FastAPI()  # Deliberately not create_app(): no schedulers/startup cleanup.
    app.include_router(storage_api.create_router())
    app.include_router(system.create_router("synthetic-integration"))
    app.include_router(tasks_api.create_router())
    with TestClient(app, headers={"Authorization": f"Bearer {AUTH}"}, follow_redirects=False) as client:
        yield SimpleNamespace(
            root=tmp_path, data_dir=data_dir, store=store, storage=storage, manager=manager,
            logs=logs, clock=clock, cloud=cloud, remote=remote, tasks=tasks, client=client,
        )


def call(env, method, path, body=None, *, status=200):
    response = env.client.request(method, path, json=body)
    assert_redacted(response.text)
    assert response.status_code == status, f"{method} {path}: HTTP {response.status_code}, expected {status}"
    return response.json()


def save_settings(env, settings, entry="storage", *, status=200):
    if entry == "general":
        return call(env, "POST", "/api/settings", {"image_storage": settings}, status=status)
    return call(env, "PUT", BASE, settings, status=status)


def onboard(env):
    """Every eligibility bit must be earned through the real admin endpoints."""
    draft = call(env, "PUT", BASE, {
        "enabled": True, "mode": "r2", "retention_minutes": 1,
        "r2_accounts": [{"id": ACCOUNT, "enabled": False}],
    })
    assert draft["accounts"][0]["status"] == "disabled"
    assert not draft["settings"]["r2_accounts"][0]["has_api_token"]
    settings = settings_fixture()
    settings["retention_minutes"] = 1
    settings["r2_accounts"][0].update(enabled=False, domains=[], active_domain_id="")
    call(env, "PUT", BASE, settings)
    assert env.cloud.calls == env.cloud.usage_calls == env.remote.calls == []

    assert call(env, "GET", ACCOUNT_PATH + "/zones")["items"] == [{"id": ZONE, "name": "example.test"}]
    for host in HOSTS:
        added = call(env, "POST", ACCOUNT_PATH + "/domains", {"domain": host, "zone_id": ZONE})["item"]
        assert added["enabled"] is False
        assert env.cloud.domains[host]["enabled"] is False
    call(env, "PATCH", ACCOUNT_PATH + "/domains/" + HOSTS[0], {"enabled": True, "active": True}, status=400)
    assert not any(item["enabled"] for item in env.cloud.domains.values())

    user_rules = {phase: copy.deepcopy(value["rules"][0]) for phase, value in env.cloud.rulesets.items()}
    assert call(env, "PUT", ACCOUNT_PATH + "/protection", PROTECTION)["result"]["ok"] is True
    for phase, ruleset in env.cloud.rulesets.items():
        assert ruleset["rules"][0] == user_rules[phase]
        assert len(ruleset["rules"]) == 2
    assert env.cloud.managed is False
    for host in HOSTS:
        changes = {"enabled": True, **({"active": True} if host == HOSTS[0] else {})}
        call(env, "PATCH", ACCOUNT_PATH + "/domains/" + host, changes)
    public = call(env, "GET", BASE)["settings"]
    public["r2_accounts"][0]["enabled"] = True
    saved = call(env, "PUT", BASE, public)
    assert saved["accounts"][0]["status"] == "usage_stale"
    with pytest.raises(storage_module.ImageStorageError):
        env.storage.require_upload_available()
    usage = call(env, "POST", ACCOUNT_PATH + "/usage/refresh")["result"]
    assert usage["complete"] is True
    assert (usage["cloud_a"], usage["cloud_b"], usage["effective_a"]) == (20, 1, 20)
    assert env.storage.account_status(ACCOUNT) == "eligible"
    assert env.remote.calls == []
    env.clock[0] += timedelta(seconds=1)


def complete_task(env, stored):
    env.tasks._update_task(f"admin:{TASK_ID}", status="success", data=[{
        "url": stored.url, "revised_prompt": "synthetic image",
        "_storage_reference": {
            "image_id": stored.image_id, "account_id": stored.account_id, "expires_at": stored.expires_at,
        },
    }])


def task_image(env):
    result = call(env, "GET", f"/api/image-tasks?ids={TASK_ID}")
    assert result["missing_ids"] == []
    return result["items"][0]["data"][0]


def resolve(env, stored):
    return call(env, "POST", BASE + "/resolve", {"images": [{
        "image_id": stored.image_id, "account_id": stored.account_id,
        "url": "https://untrusted.example.test/ignored",  # Never an authoritative URL.
    }]})["items"][0]


def assert_metadata_only(env):
    assert not (env.data_dir / "images").exists()
    assert not (env.data_dir / "image_thumbnails").exists()
    # The private config alone may contain R2 credentials, never index/tasks/logs.
    for path in env.data_dir.rglob("*"):
        if path.is_file():
            assert path.suffix in {".json", ".jsonl"}
            assert_redacted(path.read_text(encoding="utf-8"))


def test_onboarding_upload_task_reference_domain_switch_and_expiry(integrated, monkeypatch):
    env = integrated
    onboard(env)
    payload = image_bytes()

    def inspect_pending(key):
        document = json.loads(env.storage.index_file.read_text(encoding="utf-8"))
        item = document["items"][key]
        assert item["pending"] is True
        assert item["object_key"] == item["image_id"] == key
        assert item["account_id"] == ACCOUNT
        assert not item["local"] and not item["webdav"]
        assert document["r2_usage"][ACCOUNT]["a_local"] == 21
        assert env.storage.account_usage(ACCOUNT)["pending_bytes"] == len(payload)
        assert env.storage.list_items("") == []

    env.remote.on_put = inspect_pending
    stored = env.storage.save(payload)
    assert stored.storage == "r2" and stored.image_id == stored.rel
    assert stored.url == f"https://{HOSTS[0]}/{stored.rel}"
    assert env.storage.metadata(stored.rel)["pending"] is False
    assert env.remote.uploads == [{
        "content_type": "image/png", "metadata": {"expires-at": stored.expires_at},
        "cache_control": "public, max-age=0, s-maxage=60, must-revalidate",
    }]
    assert env.storage.account_usage(ACCOUNT)["local_bytes"] == len(payload)
    complete_task(env, stored)
    persisted = env.tasks.path.read_text(encoding="utf-8")
    reference = {"image_id": stored.image_id, "account_id": ACCOUNT, "expires_at": stored.expires_at}
    assert json.loads(persisted)["tasks"][0]["data"] == [{**reference, "revised_prompt": "synthetic image"}]
    assert stored.url not in persisted and "b64_json" not in persisted
    assert task_image(env)["url"] == stored.url

    call(env, "PATCH", ACCOUNT_PATH + "/domains/" + HOSTS[1], {"active": True})
    current_url = f"https://{HOSTS[1]}/{stored.rel}"
    # Re-open real persistence, not just an in-memory task result.
    reloaded_tasks = task_module.ImageTaskService(env.tasks.path)
    monkeypatch.setattr(tasks_api, "image_task_service", reloaded_tasks)
    reloaded_storage = storage_module.ImageStorageService(env.storage.index_file)
    assert reloaded_storage.public_url(stored.rel) == current_url
    assert task_image(env)["url"] == resolve(env, stored)["url"] == current_url
    assert env.tasks.path.read_text(encoding="utf-8") == persisted
    listing = call(env, "GET", "/api/images")["items"]
    assert len(listing) == 1 and listing[0]["thumbnail_url"] == listing[0]["url"] == current_url
    for prefix in ("/images/", "/image-thumbnails/"):
        response = env.client.get(prefix + stored.rel)
        assert response.status_code == 307
        assert response.headers["location"] == current_url
        assert response.headers["cache-control"] == "no-store"
    assert env.remote.calls == [("PUT", "synthetic-images", stored.rel)]
    audit = call(env, "GET", "/api/logs?type=image-storage")["items"][0]["detail"]
    assert (audit["old_domain"], audit["new_domain"], audit["actor"]) == (*HOSTS, "admin")

    env.clock[0] += timedelta(minutes=2)
    assert task_image(env)["expired"] is True
    assert resolve(env, stored)["url"] == ""
    call(env, "GET", "/images/" + stored.rel, status=410)
    assert call(env, "POST", BASE + "/cleanup")["result"] == {"removed": 1, "failed": 0, "pending": 0}
    assert reloaded_storage.metadata(stored.rel) is None
    assert env.remote.objects == {}
    assert env.remote.calls[-1] == ("DELETE", "synthetic-images", stored.rel)
    assert env.storage.account_usage(ACCOUNT)["local_bytes"] == 0
    assert env.storage.get_r2_usage(ACCOUNT)["a_local"] == 21
    assert task_image(env)["url"] == "" and resolve(env, stored)["expired"] is True
    assert env.tasks.path.read_text(encoding="utf-8") == persisted
    assert_metadata_only(env)


@pytest.mark.parametrize("mode", ["generate", "edit"])
def test_upload_receipt_survives_old_domain_removal_before_task_persistence(integrated, monkeypatch, mode):
    env = integrated
    onboard(env)
    payload = image_bytes()
    formatted = []
    protocol = task_module.openai_v1_image_edit if mode == "edit" else task_module.openai_v1_image_generations

    def outputs(request):
        data = conversation._format_image_bytes([payload], request.prompt, request.response_format, request.base_url)
        formatted.extend(data)
        # The upload URL is no longer a configured domain when the real handler
        # returns to _run_task. Only the receipt can identify this stored result.
        call(env, "PATCH", ACCOUNT_PATH + "/domains/" + HOSTS[1], {"active": True})
        call(env, "DELETE", ACCOUNT_PATH + "/domains/" + HOSTS[0])
        assert all(domain["domain"] != HOSTS[0] for domain in env.storage.account(ACCOUNT)["domains"])
        yield conversation.ImageOutput(kind="result", model=request.model, index=1, total=1, data=data)

    monkeypatch.setattr(protocol, "stream_image_outputs_with_pool", outputs)
    monkeypatch.setattr(protocol, "count_text_tokens", lambda *_args: 1)
    if mode == "edit":
        monkeypatch.setattr(protocol, "count_image_inputs_tokens", lambda *_args: 1)
    env.tasks._run_task(f"admin:{TASK_ID}", mode, {
        "prompt": "synthetic image", "model": "gpt-image-2", "response_format": "url",
        **({"images": [(payload, "synthetic-input.png", "image/png")]} if mode == "edit" else {}),
    }, {"id": "admin", "role": "admin"}, "gpt-image-2")

    assert len(formatted) == 1
    reference = formatted[0]["_storage_reference"]
    old_url = formatted[0]["url"]
    assert old_url == f"https://{HOSTS[0]}/{reference['image_id']}"
    persisted = env.tasks.path.read_text(encoding="utf-8")
    task = json.loads(persisted)["tasks"][0]
    assert task["status"] == "success"
    assert task["data"] == [{**reference, "revised_prompt": "synthetic image"}]
    assert old_url not in persisted and "_storage_reference" not in persisted and "b64_json" not in persisted
    reloaded = task_module.ImageTaskService(env.tasks.path)
    monkeypatch.setattr(tasks_api, "image_task_service", reloaded)
    public_image = task_image(env)
    assert public_image["image_id"] == reference["image_id"]
    assert public_image["url"] == f"https://{HOSTS[1]}/{reference['image_id']}"
    assert env.tasks.path.read_text(encoding="utf-8") == persisted
    assert env.remote.calls == [("PUT", "synthetic-images", reference["image_id"])]
    assert_metadata_only(env)


def test_b_pause_closes_all_actual_bindings_and_only_explicit_restore_reopens(integrated):
    env = integrated
    onboard(env)
    stored = env.storage.save(image_bytes())
    complete_task(env, stored)
    unknown = "unregistered.example.test"
    env.cloud.domains[unknown] = {**copy.deepcopy(env.cloud.domains[HOSTS[0]]), "domain": unknown}
    env.cloud.managed = True  # Simulate an external public entry not in local config.
    env.cloud.gates_at_disable.clear()
    env.cloud.usage_b = 80
    usage = call(env, "POST", ACCOUNT_PATH + "/usage/refresh")["result"]
    assert usage["access_paused"] is usage["shutdown_ok"] is True
    assert usage["b_warning"] is True
    assert env.cloud.gates_at_disable == [True, True, True]
    assert env.cloud.managed is False
    assert not any(domain["enabled"] for domain in env.cloud.domains.values())
    assert all(domain["enabled"] for domain in env.storage.account(ACCOUNT)["domains"])
    assert task_image(env)["unavailable"] is True
    assert resolve(env, stored)["url"] == ""
    calls_before = list(env.remote.calls)
    with pytest.raises(storage_module.ImageStorageError):
        env.storage.save(image_bytes())
    for operation in (env.storage.public_url, env.storage.get_bytes):
        with pytest.raises(HTTPException) as error:
            operation(stored.rel)
        assert error.value.status_code == 403
    for path in ("/images/", "/image-thumbnails/", "/api/images/download/"):
        call(env, "GET", path + stored.rel, status=403)
    call(env, "POST", "/api/images/download", {"paths": [stored.rel]}, status=403)
    assert env.remote.calls == calls_before

    for changes in ({"active": True}, {"enabled": True}):
        call(env, "PATCH", ACCOUNT_PATH + "/domains/" + HOSTS[1], changes, status=400)
    save_settings(env, {"r2_accounts": [{"id": ACCOUNT, "active_domain_id": HOSTS[1]}]}, "general", status=400)
    for enabled in (False, True):
        save_settings(env, {"r2_accounts": [{"id": ACCOUNT, "enabled": enabled}]}, "general")
        assert env.storage.account_status(ACCOUNT) == "access_paused"
    assert env.storage.account(ACCOUNT)["active_domain_id"] == HOSTS[0]
    call(env, "POST", ACCOUNT_PATH + "/access/restore", status=400)
    assert not any(domain["enabled"] for domain in env.cloud.domains.values())

    env.cloud.usage_b = 1
    call(env, "POST", ACCOUNT_PATH + "/usage/refresh")
    assert env.storage.account_status(ACCOUNT) == "access_paused"
    assert not any(domain["enabled"] for domain in env.cloud.domains.values())
    queries = len(env.cloud.usage_calls)
    assert call(env, "POST", ACCOUNT_PATH + "/access/restore")["result"]["ok"] is True
    assert len(env.cloud.usage_calls) == queries + 1  # Restore must check fresh usage itself.
    assert env.storage.account_status(ACCOUNT) == "eligible"
    assert env.storage.get_r2_usage(ACCOUNT)["access_paused"] is False
    assert all(env.cloud.domains[host]["enabled"] for host in HOSTS)
    assert env.cloud.domains[unknown]["enabled"] is False and env.cloud.managed is False
    assert resolve(env, stored)["url"] == task_image(env)["url"] == stored.url
    assert env.remote.calls == calls_before  # No copy, download or fresh upload on restore.
    assert_metadata_only(env)


def test_general_settings_cannot_bypass_manual_active_domain_switch(integrated):
    env = integrated
    onboard(env)
    stored = env.storage.save(image_bytes())
    before = env.store.path.read_bytes()
    calls_before = copy.deepcopy(env.cloud.calls)
    public = call(env, "GET", "/api/settings")["config"]
    public["image_storage"]["r2_accounts"][0]["active_domain_id"] = HOSTS[1]
    call(env, "POST", "/api/settings", public, status=400)
    assert env.store.path.read_bytes() == before
    assert env.storage.public_url(stored.rel) == stored.url
    assert env.cloud.calls == calls_before
    assert env.storage.account_status(ACCOUNT) == "eligible"

    # Unrelated settings and blank-secret round trips must not disable R2.
    call(env, "POST", "/api/settings", {"image_retention_days": 12})
    public = call(env, "GET", "/api/settings")["config"]
    call(env, "POST", "/api/settings", public)
    assert env.store.get_image_storage_settings()["mode"] == "r2"
    assert env.storage.account_status(ACCOUNT) == "eligible"


@pytest.mark.parametrize("changed", ["rate_limit_requests", "restrict_methods", "domains", "cors_origins"])
def test_general_settings_invalidate_protection_without_applying_or_bypassing_it(integrated, changed):
    env = integrated
    onboard(env)
    public = call(env, "GET", "/api/settings")["config"]["image_storage"]
    account = public["r2_accounts"][0]
    if changed == "domains":
        account["domains"][1]["zone_id"] = "c" * 32
    elif changed == "cors_origins":
        public["cors_origins"] = ["https://other.example.test"]
    else:
        account[changed] = 140 if changed == "rate_limit_requests" else False
    calls_before = copy.deepcopy(env.cloud.calls)
    save_settings(env, public, "general")
    usage = env.storage.get_r2_usage(ACCOUNT)
    assert usage["verified_domains"] == []
    assert usage["domains_checked"] is usage["domains_safe"] is False
    assert env.storage.account_status(ACCOUNT) == "domains_unprotected"
    assert env.cloud.calls == calls_before
    with pytest.raises(storage_module.ImageStorageError):
        env.storage.save(image_bytes())
    call(env, "PATCH", ACCOUNT_PATH + "/domains/" + HOSTS[1], {"active": True}, status=400)
    assert env.storage.account(ACCOUNT)["active_domain_id"] == HOSTS[0]
    assert env.remote.calls == []
    assert_metadata_only(env)


@pytest.mark.parametrize("entry", ["storage", "general"])
def test_real_secret_roundtrip_and_error_paths_do_not_expose_credentials(integrated, entry):
    env = integrated
    onboard(env)
    public = call(env, "GET", BASE)["settings"]
    for field in SECRET_FIELDS:
        assert public["r2_accounts"][0][field] == ""
        assert public["r2_accounts"][0]["has_" + field] is True
    save_settings(env, public, entry)
    for field in SECRET_FIELDS:
        public["r2_accounts"][0].pop(field)
    save_settings(env, public, entry)
    reloaded = config_module.ConfigStore(env.store.path).get_image_storage_settings()["r2_accounts"][0]
    assert all(reloaded[field] == value for field, value in zip(SECRET_FIELDS, SECRET_VALUES))
    assert_redacted(env.store.get())

    invalid = copy.deepcopy(public)
    invalid["r2_accounts"][0]["capacity_bytes"] = SECRET_VALUES[-1]
    before = env.store.path.read_bytes()
    save_settings(env, invalid, entry, status=400)
    assert env.store.path.read_bytes() == before
    env.cloud.usage_error = CloudflareAPIError(" ".join(SECRET_VALUES), status_code=403, codes=(10000,))
    error = call(env, "POST", ACCOUNT_PATH + "/usage/refresh", status=400)
    assert "403" in error["detail"]["error"]
    assert env.storage.get_r2_usage(ACCOUNT)["complete"] is False
    env.cloud.usage_error = None
    env.cloud.rules_error = True
    call(env, "PUT", ACCOUNT_PATH + "/protection", PROTECTION, status=400)
    for path in (BASE, "/api/settings", ACCOUNT_PATH + "/domains", "/api/logs"):
        call(env, "GET", path)
    assert_metadata_only(env)


@pytest.mark.parametrize("entry", ["storage", "general"])
def test_inflight_and_uncertain_pending_put_exclude_bucket_change_until_cleanup(integrated, entry):
    env = integrated
    onboard(env)
    entered, release = Event(), Event()
    payload = image_bytes()
    env.remote.upload_error = R2Error(" ".join(SECRET_VALUES))

    def wait_in_transport(key):
        entered.set()
        assert release.wait(10), "fixture PUT was not released"

    env.remote.on_put = wait_in_transport
    replacement = {"r2_accounts": [{"id": ACCOUNT, "bucket": "replacement-images"}]}
    before = env.store.path.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as workers:
        upload = workers.submit(env.storage.save, payload)
        try:
            assert entered.wait(5), "fixture PUT was not reached"
            document = json.loads(env.storage.index_file.read_text(encoding="utf-8"))
            key, pending = next(iter(document["items"].items()))
            assert pending["pending"] is True and pending["size"] == len(payload)
            # Real config validation runs while fake network I/O is blocked. It
            # must see the durable reservation, not wait on an I/O-held index lock.
            save_settings(env, replacement, entry, status=400)
            for candidate in (replacement, {"r2_accounts": []}, {
                "r2_accounts": [{"id": ACCOUNT, "account_id": "c" * 32}],
            }):
                with pytest.raises(ValueError):
                    env.store.update({"image_storage": candidate})
            assert env.store.path.read_bytes() == before
            assert env.storage.account(ACCOUNT)["bucket"] == "synthetic-images"
            usage = env.storage.account_usage(ACCOUNT)
            assert (usage["pending_count"], usage["pending_bytes"], usage["image_count"], usage["a_local"]) == (1, len(payload), 0, 21)
        finally:
            release.set()
        with pytest.raises(storage_module.ImageStorageError, match="^R2 image upload failed$"):
            upload.result(timeout=5)

    # Failed PUT retains its reservation across service reload and cannot become
    # a URL result. Identity changes remain forbidden even after network returns.
    reopened = storage_module.ImageStorageService(env.storage.index_file)
    assert reopened.metadata(key)["pending"] is True
    assert reopened.list_items("") == []
    with pytest.raises(HTTPException) as error:
        reopened.public_url(key)
    assert error.value.status_code == 404
    assert ("synthetic-images", key) in env.remote.objects
    save_settings(env, replacement, entry, status=400)
    assert env.store.path.read_bytes() == before
    assert call(env, "POST", BASE + "/cleanup")["result"] == {"removed": 0, "failed": 0, "pending": 0}
    env.clock[0] += timedelta(minutes=2)
    assert call(env, "POST", BASE + "/cleanup")["result"] == {"removed": 1, "failed": 0, "pending": 0}
    assert env.remote.calls == [("PUT", "synthetic-images", key), ("DELETE", "synthetic-images", key)]
    assert env.remote.objects == {} and reopened.metadata(key) is None
    save_settings(env, replacement, entry)
    persisted = config_module.ConfigStore(env.store.path).get_image_storage_settings()["r2_accounts"][0]
    assert persisted["bucket"] == "replacement-images"
    assert env.storage.account_status(ACCOUNT) == "domains_unprotected"
    assert env.storage.get_r2_usage(ACCOUNT)["a_local"] == 21
    assert_metadata_only(env)
