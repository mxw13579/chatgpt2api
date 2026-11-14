"""Synthetic service/pipeline regressions; run only via test/run_isolated.py.

No cloud, real application, authentication or production data is used.
"""
from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from services import image_service, image_task_service as task_module
from services import r2_management_service as management_module
from services.image_storage_service import ImageStorageError, StoredImage
from services.protocol import conversation

IMAGE = b"synthetic-image-bytes"
REL = "images/2099/01/01/synthetic.png"
REFERENCE = {"image_id": REL, "account_id": "synthetic-account", "expires_at": "2099-01-01T01:00:00+00:00"}
URL = f"https://first.synthetic.invalid/{REL}"
OWNER = {"id": "synthetic-owner", "role": "admin"}


@pytest.fixture
def service_env(monkeypatch, tmp_path):
    settings = {
        "mode": "r2", "cleanup_interval_seconds": 60,
        "r2_accounts": [{"id": "synthetic-account"}],
    }

    class SyntheticConfig:
        cleanup_old_images = Mock()
        image_min_free_mb = 500

        def get_image_storage_settings(self):
            return settings

        @property
        def images_dir(self):
            raise AssertionError("directory-creating config property used")

        @property
        def image_thumbnails_dir(self):
            raise AssertionError("directory-creating config property used")

    storage = Mock(spec=["metadata", "_load_index", "public_url", "get_bytes", "has_local", "delete", "list_items"])
    storage.metadata.return_value = None
    storage.has_local.return_value = False
    storage.public_url.return_value = URL
    storage.get_bytes.return_value = IMAGE
    storage.list_items.return_value = []
    config = SyntheticConfig()
    monkeypatch.setattr(image_service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(image_service, "config", config)
    monkeypatch.setattr(image_service, "image_storage_service", storage)
    monkeypatch.setattr(image_service, "load_tags", Mock(return_value={}))
    monkeypatch.setattr(image_service, "remove_tags", Mock())
    monkeypatch.setattr(image_service, "logger", Mock())
    return SimpleNamespace(root=tmp_path, storage=storage, config=config, settings=settings)


@pytest.mark.parametrize("response_format", ["url", "b64_json"])
def test_only_url_generation_checks_storage_before_starting_upstream(monkeypatch, response_format):
    preflight = Mock(side_effect=ImageStorageError("no eligible storage"))
    generate = Mock(return_value=iter(()))
    monkeypatch.setattr(conversation.image_storage_service, "require_upload_available", preflight)
    monkeypatch.setattr(conversation, "_generate_single_image", generate)
    request = conversation.ConversationRequest(model="gpt-image-2", prompt="synthetic", response_format=response_format)
    if response_format == "url":
        with pytest.raises(ImageStorageError):
            list(conversation.stream_image_outputs_with_pool(request))
        generate.assert_not_called()
    else:
        assert list(conversation.stream_image_outputs_with_pool(request)) == []
        preflight.assert_not_called()
        generate.assert_called_once()


@pytest.mark.parametrize("mode", ["r2", "local", "webdav"])
@pytest.mark.parametrize("handler", [image_service.get_image_response, image_service.get_thumbnail_response])
def test_anonymous_r2_content_always_redirects_through_current_domain(service_env, mode, handler):
    env = service_env
    env.settings["mode"] = mode
    env.storage.metadata.return_value = {"storage": "r2", **REFERENCE}
    response = handler(REL)
    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == URL
    assert response.headers["cache-control"] == "no-store"
    env.storage.public_url.assert_called_once_with(REL)
    env.storage.get_bytes.assert_not_called()
    env.storage.has_local.assert_not_called()
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("status", [403, 404, 410])
@pytest.mark.parametrize("handler", [image_service.get_image_response, image_service.get_thumbnail_response])
def test_anonymous_r2_unavailable_never_falls_back_to_proxy(service_env, status, handler):
    env = service_env
    env.storage.metadata.return_value = {"storage": "r2"}
    env.storage.public_url.side_effect = HTTPException(status, "synthetic-unavailable")
    with pytest.raises(HTTPException) as error:
        handler(REL)
    assert error.value.status_code == status
    env.storage.get_bytes.assert_not_called()
    env.storage.has_local.assert_not_called()
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("local", [False, True])
def test_r2_mode_legacy_thumbnail_serves_original_without_creating_files(service_env, monkeypatch, local):
    env = service_env
    rel = "2099/01/01/legacy.png"
    original = env.root / "images" / rel
    if local:
        original.parent.mkdir(parents=True)
        original.write_bytes(IMAGE)
    env.storage.has_local.return_value = local
    ensure = Mock(side_effect=AssertionError("thumbnail generation must not run"))
    monkeypatch.setattr(image_service, "ensure_thumbnail", ensure)
    response = image_service.get_thumbnail_response(rel)
    if local:
        assert isinstance(response, FileResponse)
        assert Path(response.path) == original
        env.storage.get_bytes.assert_not_called()
    else:
        assert response.body == IMAGE
        env.storage.get_bytes.assert_called_once_with(rel)
        assert not (env.root / "images").exists()
    ensure.assert_not_called()
    assert not (env.root / "image_thumbnails").exists()


@pytest.mark.parametrize("operation", [lambda: image_service.ensure_thumbnail(REL), image_service.compress_images])
def test_r2_mode_disables_local_image_writers(service_env, operation):
    with pytest.raises(HTTPException) as error:
        operation()
    assert error.value.status_code == 409
    assert list(service_env.root.iterdir()) == []


def test_r2_list_preserves_expiry_and_does_not_clean_legacy_or_add_proxy_urls(service_env):
    env = service_env
    env.storage.list_items.return_value = [
        {"path": REL, "storage": "r2", "date": "2099-01-01", "url": URL, **REFERENCE},
        {"path": "images/expired.png", "storage": "r2", "date": "2099-01-01", "url": "", "expired": True},
        {"path": "images/paused.png", "storage": "r2", "date": "2099-01-01", "url": "", "unavailable": True},
        {"path": "legacy.png", "storage": "local", "date": "2099-01-01", "url": "/images/legacy.png"},
    ]
    items = image_service.list_images("https://app.synthetic.invalid")["items"]
    assert [item["thumbnail_url"] for item in items] == [URL, "", "", "/images/legacy.png"]
    assert items[0]["account_id"] == REFERENCE["account_id"]
    assert items[1]["url"] == items[2]["url"] == ""
    env.config.cleanup_old_images.assert_not_called()
    env.storage.delete.assert_not_called()
    env.storage.get_bytes.assert_not_called()
    assert list(env.root.iterdir()) == []


def test_r2_stats_use_metadata_without_local_directory_scan(service_env, monkeypatch):
    env = service_env
    env.storage.list_items.return_value = [
        {"storage": "r2", "size": 100}, {"storage": "r2", "size": 200, "expired": True},
        {"storage": "local", "local": True, "size": 40},
        {"storage": "webdav", "local": False, "size": 90},
    ]
    legacy_stats = Mock(return_value={"legacy_thumbnail_count": 2, "legacy_payload_count": 3})
    monkeypatch.setattr(image_service, "legacy_storage_stats", legacy_stats)
    monkeypatch.setattr(Path, "rglob", Mock(side_effect=AssertionError("local image scan forbidden")))
    stats = image_service.storage_stats()
    assert stats["image_count"] == 2
    assert stats["image_size_bytes"] == 300
    assert stats["legacy_image_count"] == 1
    assert stats["legacy_size_bytes"] == 40
    assert stats["legacy_thumbnail_count"] == 2
    assert stats["legacy_payload_count"] == 3
    legacy_stats.assert_called_once_with()
    assert list(env.root.iterdir()) == []


def test_empty_legacy_stats_do_not_create_directories(service_env):
    assert image_service.legacy_storage_stats() == {"legacy_thumbnail_count": 0, "legacy_payload_count": 0}
    assert list(service_env.root.iterdir()) == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_r2_legacy_stats_count_pending_files_and_payloads_without_mutation(service_env, monkeypatch, wrapped):
    env = service_env
    thumbnails = env.root / "image_thumbnails"
    (thumbnails / "nested").mkdir(parents=True)
    (thumbnails / "old.png").write_bytes(IMAGE)
    (thumbnails / "nested" / "old.png").write_bytes(IMAGE)
    tasks = [
        {"id": "legacy", "owner_id": OWNER["id"], "data": [
            {"b64_json": "c3ludGhldGlj", "url": "data:image/png;base64,c3ludGhldGlj"},
            {"b64_json": ""},
            {"url": " DaTa:image/png;base64,c3ludGhldGlj "},
            {"url": "https://legacy.synthetic.invalid/old.png"},
            dict(REFERENCE),
            "legacy placeholder",
        ]},
        {"id": "queued", "owner_id": OWNER["id"]},
        {"id": "no-result", "owner_id": OWNER["id"], "data": None},
    ]
    task_path = env.root / "image_tasks.json"
    saved = json.dumps({"tasks": tasks} if wrapped else tasks)
    task_path.write_text(saved, encoding="utf-8")
    stats = image_service.storage_stats()
    assert stats["legacy_thumbnail_count"] == 2
    assert stats["legacy_payload_count"] == 3
    assert task_path.read_text(encoding="utf-8") == saved
    assert (thumbnails / "old.png").read_bytes() == IMAGE
    assert (thumbnails / "nested" / "old.png").read_bytes() == IMAGE
    assert not (env.root / "images").exists()
    env.storage.delete.assert_not_called()
    env.config.cleanup_old_images.assert_not_called()


def test_zip_reads_all_storage_types_into_memory_and_keeps_duplicate_names(service_env):
    env = service_env
    payloads = {"local/same.png": b"local", "remote/same.png": b"webdav", REL: IMAGE}

    def get_bytes(rel):
        if rel not in payloads:
            raise HTTPException(404, "synthetic missing image")
        return payloads[rel]

    env.storage.get_bytes.side_effect = get_bytes
    buffer = image_service.download_images_zip(["missing.png", *payloads])
    assert isinstance(buffer, io.BytesIO)
    with zipfile.ZipFile(buffer) as archive:
        assert archive.namelist() == ["same.png", "same_2.png", "synthetic.png"]
        assert [archive.read(name) for name in archive.namelist()] == list(payloads.values())
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("status", [403, 410])
def test_zip_never_skips_paused_or_expired_images(service_env, status):
    service_env.storage.get_bytes.side_effect = [IMAGE, HTTPException(status, "synthetic unavailable")]
    with pytest.raises(HTTPException) as error:
        image_service.download_images_zip(["first.png", REL])
    assert error.value.status_code == status
    assert list(service_env.root.iterdir()) == []


def test_authenticated_single_download_enforces_storage_read_gate(service_env):
    service_env.storage.get_bytes.side_effect = HTTPException(403, "synthetic access paused")
    with pytest.raises(HTTPException) as error:
        image_service.get_image_download_response(REL)
    assert error.value.status_code == 403


def test_explicit_r2_delete_does_not_create_or_scan_legacy_directories(service_env, monkeypatch):
    env = service_env
    env.storage.metadata.return_value = {"storage": "r2", "expired": True}
    env.storage.delete.return_value = True
    cleanup = Mock(side_effect=AssertionError("local cleanup must not run"))
    monkeypatch.setattr(image_service, "_cleanup_empty_dirs", cleanup)
    assert image_service.delete_images([REL]) == {"removed": 1}
    env.storage.delete.assert_called_once_with(REL)
    cleanup.assert_not_called()
    assert list(env.root.iterdir()) == []


def test_explicit_legacy_cleanup_keeps_remote_objects_and_r2_metadata(service_env, monkeypatch):
    env = service_env
    originals = env.root / "images"
    originals.mkdir()
    for name in ("local.png", "both.png", "r2.png"):
        (originals / name).write_bytes(IMAGE)
    thumbnails = env.root / "image_thumbnails"
    thumbnails.mkdir()
    (thumbnails / "old.png").write_bytes(IMAGE)
    metadata = {
        "local.png": {"storage": "local", "local": True},
        "both.png": {"storage": "both", "local": True, "webdav": True},
        "r2.png": {"storage": "r2", **REFERENCE},
    }
    env.storage._load_index.return_value = metadata
    clear = Mock(return_value={"tasks": 1, "payloads": 2})
    monkeypatch.setattr(task_module.image_task_service, "clear_legacy_image_payloads", clear)
    assert image_service.cleanup_legacy_images() == {"removed": 2, "thumbnails": 1, "tasks": 1, "payloads": 2}
    env.storage.delete.assert_not_called()
    env.storage.list_items.assert_called_once_with("")
    clear.assert_called_once_with()
    assert not (originals / "local.png").exists()
    assert not (originals / "both.png").exists()
    assert (originals / "r2.png").read_bytes() == IMAGE
    assert not list(thumbnails.rglob("*"))


def test_maintenance_uses_one_loop_and_dynamic_r2_interval(service_env, monkeypatch):
    env = service_env
    env.settings["cleanup_interval_seconds"] = 15
    manager = SimpleNamespace(maintenance_tick=Mock())
    monkeypatch.setattr(management_module, "r2_management_service", manager)
    clock = [0.0]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 3

    def tick():
        env.settings["cleanup_interval_seconds"] = 30

    manager.maintenance_tick.side_effect = tick
    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [15, 30, 30]
    assert manager.maintenance_tick.call_count == 2
    env.config.cleanup_old_images.assert_not_called()
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("mode", ["r2", "local", "webdav"])
def test_maintenance_failure_is_sanitized_without_blocking_local_cadence(service_env, monkeypatch, mode):
    env = service_env
    env.settings["mode"] = mode
    manager = SimpleNamespace(maintenance_tick=Mock(side_effect=RuntimeError("synthetic-private-error")))
    monkeypatch.setattr(management_module, "r2_management_service", manager)
    clock = [0.0]
    waits = []
    local_runs = []
    env.config.cleanup_old_images.side_effect = lambda: local_runs.append(clock[0])

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 31

    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(image_service.shutil, "disk_usage", Mock(return_value=SimpleNamespace(free=1024 ** 4)))
    thumbnails = Mock()
    monkeypatch.setattr(image_service, "cleanup_image_thumbnails", thumbnails)
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [60] * 31
    assert manager.maintenance_tick.call_count == 30
    assert image_service.logger.warning.call_count == 30
    assert "synthetic-private-error" not in str(image_service.logger.mock_calls)
    assert local_runs == ([] if mode == "r2" else [1800])
    assert thumbnails.call_count == len(local_runs)


@pytest.mark.parametrize("mode", ["r2", "local", "webdav"])
def test_maintenance_after_mode_switch_keeps_r2_and_local_deadlines_independent(service_env, monkeypatch, mode):
    env = service_env
    env.settings["cleanup_interval_seconds"] = 70
    clock = [0.0]
    waits = []
    ticks = []
    local_runs = []
    env.config.cleanup_old_images.side_effect = lambda: local_runs.append(clock[0])
    manager = SimpleNamespace(maintenance_tick=Mock(side_effect=lambda: ticks.append((clock[0], env.settings["mode"]))))
    monkeypatch.setattr(management_module, "r2_management_service", manager)

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if clock[0] >= 120:
            env.settings["mode"] = mode
        return clock[0] >= 1860

    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(image_service.shutil, "disk_usage", Mock(return_value=SimpleNamespace(free=1024 ** 4)))
    thumbnails = Mock()
    monkeypatch.setattr(image_service, "cleanup_image_thumbnails", thumbnails)
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert all(0 < seconds <= 60 for seconds in waits)
    assert ticks == [(70, "r2"), *[(at, mode) for at in range(140, 1821, 70)]]
    assert local_runs == ([] if mode == "r2" else [1800])
    assert thumbnails.call_count == len(local_runs)
    assert list(env.root.iterdir()) == []


@pytest.mark.parametrize("mode", ["r2", "local", "webdav"])
def test_maintenance_without_r2_accounts_keeps_legacy_thirty_minute_cadence(service_env, monkeypatch, mode):
    env = service_env
    env.settings.update(mode=mode, r2_accounts=[])
    manager = SimpleNamespace(maintenance_tick=Mock())
    monkeypatch.setattr(management_module, "r2_management_service", manager)
    clock = [0.0]
    waits = []
    local_runs = []
    env.config.cleanup_old_images.side_effect = lambda: local_runs.append(clock[0])

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 61

    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(image_service.shutil, "disk_usage", Mock(return_value=SimpleNamespace(free=1024 ** 4)))
    thumbnails = Mock()
    monkeypatch.setattr(image_service, "cleanup_image_thumbnails", thumbnails)
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [60] * 61
    assert local_runs == ([] if mode == "r2" else [1800, 3600])
    assert thumbnails.call_count == len(local_runs)
    manager.maintenance_tick.assert_not_called()


def test_scheduler_starts_one_loop_with_lifespan_stop_event(service_env, monkeypatch):
    thread = Mock()
    constructor = Mock(return_value=thread)
    monkeypatch.setattr(image_service.threading, "Thread", constructor)
    stop = Mock()
    assert image_service.start_image_cleanup_scheduler(stop) is thread
    constructor.assert_called_once_with(
        target=image_service._auto_cleanup_worker, args=(stop,), daemon=True, name="image-cleanup",
    )
    thread.start.assert_called_once_with()


@pytest.fixture
def task_env(monkeypatch, tmp_path):
    storage = Mock(spec=["resolve_reference", "save"])
    storage.resolve_reference.side_effect = lambda ref: {**ref, "url": URL}
    storage.save.return_value = StoredImage(rel=REL, url=URL, storage="r2", size=len(IMAGE), **REFERENCE)
    monkeypatch.setattr(task_module, "image_storage_service", storage)
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    monkeypatch.setattr(task_module.log_service, "add", Mock())
    service = task_module.ImageTaskService(tmp_path / "tasks.json", retention_days_getter=lambda: 30)
    return SimpleNamespace(service=service, storage=storage)


@pytest.mark.parametrize("mode", ["generate", "edit"])
def test_new_task_persists_only_reference_and_resolves_current_domain(task_env, monkeypatch, mode):
    env = task_env

    def handle(payload, *, include_storage_reference=False):
        assert payload["response_format"] == "url"
        assert include_storage_reference is True
        return {"data": [{"url": URL, "_storage_reference": dict(REFERENCE), "revised_prompt": "synthetic prompt"}]}

    handler = Mock(side_effect=handle)
    env.service.generation_handler = env.service.edit_handler = handler
    thread = Mock()
    monkeypatch.setattr(task_module.threading, "Thread", thread)
    submit = env.service.submit_edit if mode == "edit" else env.service.submit_generation
    extras = {"images": [(IMAGE, "input.png", "image/png")]} if mode == "edit" else {}
    submit(OWNER, client_task_id="task", prompt="draw", model="gpt-image-2", size=None, **extras)
    kwargs = thread.call_args.kwargs
    kwargs["target"](*kwargs["args"])
    assert handler.call_args.args[0]["response_format"] == "url"
    assert handler.call_args.kwargs == {"include_storage_reference": True}
    persisted = env.service.path.read_text(encoding="utf-8")
    data = json.loads(persisted)["tasks"][0]["data"]
    assert data == [{**REFERENCE, "revised_prompt": "synthetic prompt"}]
    assert URL not in persisted and "b64_json" not in persisted
    assert base64.b64encode(IMAGE).decode("ascii") not in persisted
    new_url = URL.replace("first.", "second.")
    env.storage.resolve_reference.side_effect = lambda ref: {**ref, "url": new_url}
    reloaded = task_module.ImageTaskService(env.service.path, retention_days_getter=lambda: 30)
    public = reloaded.list_tasks(OWNER, ["task"])["items"][0]
    assert public["data"][0]["url"] == new_url
    assert env.service.path.read_text(encoding="utf-8") == persisted
    env.storage.resolve_reference.side_effect = lambda ref: {**ref, "url": "", "expired": True}
    public = env.service.list_tasks(OWNER, ["task"])["items"][0]
    assert public["data"][0]["expired"] is True
    assert public["data"][0]["url"] == ""


@pytest.mark.parametrize("bad_image", [
    {"b64_json": "c3ludGhldGlj"},
    {"b64_json": "c3ludGhldGlj", "url": URL},
    {"url": "data:image/png;base64,c3ludGhldGlj"},
    {"b64_json": "c3ludGhldGlj", "_storage_reference": dict(REFERENCE)},
    {"url": " DaTa:image/png;base64,c3ludGhldGlj ", "_storage_reference": dict(REFERENCE)},
])
def test_new_base64_result_is_rejected_before_any_task_write(task_env, bad_image):
    env = task_env
    env.service._tasks["synthetic-owner:task"] = {"id": "task", "owner_id": OWNER["id"], "status": "running"}
    env.service._save_locked()
    before = env.service.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="require URL results"):
        env.service._update_task("synthetic-owner:task", status="success", data=[bad_image])
    assert env.service.path.read_text(encoding="utf-8") == before
    assert "data" not in env.service._tasks["synthetic-owner:task"]

    def handle(payload, *, include_storage_reference=False):
        assert include_storage_reference is True
        return {"data": [bad_image]}

    env.service.generation_handler = handle
    env.service._run_task("synthetic-owner:task", "generate", {"response_format": "url"}, OWNER, "gpt-image-2")
    persisted = env.service.path.read_text(encoding="utf-8")
    assert "c3ludGhldGlj" not in persisted and "b64_json" not in persisted
    assert json.loads(persisted)["tasks"][0]["status"] == "error"


def test_resume_poll_uses_url_storage_and_reference_without_base64(task_env, monkeypatch):
    from services import openai_backend_api

    env = task_env
    env.service._tasks["synthetic-owner:task"] = {"id": "task", "owner_id": OWNER["id"], "status": "running"}
    backend = Mock()
    backend._poll_image_results.return_value = (["synthetic-file"], [])
    backend.resolve_conversation_image_urls.return_value = ["https://upstream.synthetic.invalid/image"]
    backend.download_image_bytes.return_value = [IMAGE]
    monkeypatch.setattr(openai_backend_api, "OpenAIBackendAPI", Mock(return_value=backend))
    monkeypatch.setattr(task_module, "config", SimpleNamespace(proxy_url=None))
    monkeypatch.setattr(base64, "b64encode", Mock(side_effect=AssertionError("resume must use bytes")))
    monkeypatch.setattr(base64, "b64decode", Mock(side_effect=AssertionError("resume must use bytes")))
    env.service._run_resume_poll("synthetic-owner:task", "synthetic-conversation", 30, OWNER, "generate", "gpt-image-2")
    persisted = json.loads(env.service.path.read_text(encoding="utf-8"))["tasks"][0]
    assert persisted["status"] == "success"
    assert persisted["data"] == [{**REFERENCE, "revised_prompt": ""}]
    env.storage.save.assert_called_once_with(IMAGE, None)
    backend.close.assert_called_once_with()


def test_legacy_urls_remain_and_payloads_only_clear_on_explicit_request(task_env):
    env = task_env
    legacy = {
        "id": "legacy", "owner_id": OWNER["id"], "status": "success", "mode": "generate",
        "updated_at": "2099-01-01 00:00:00", "created_at": "2099-01-01 00:00:00",
        "data": [
            {"b64_json": "c3ludGhldGlj", "revised_prompt": "old"},
            {"url": "data:image/png;base64,c3ludGhldGlj"},
            {"url": "https://legacy.synthetic.invalid/images/old.png"},
            dict(REFERENCE),
        ],
    }
    env.service.path.write_text(json.dumps({"tasks": [legacy]}), encoding="utf-8")
    service = task_module.ImageTaskService(env.service.path, retention_days_getter=lambda: 30)
    assert service.list_tasks(OWNER, ["legacy"])["items"][0]["data"][0]["b64_json"] == "c3ludGhldGlj"
    assert "c3ludGhldGlj" in service.path.read_text(encoding="utf-8")
    assert service.clear_legacy_image_payloads() == {"tasks": 1, "payloads": 2}
    saved = service.path.read_text(encoding="utf-8")
    assert "c3ludGhldGlj" not in saved and "b64_json" not in saved
    assert json.loads(saved)["tasks"][0]["data"][2:] == legacy["data"][2:]
    assert service.clear_legacy_image_payloads() == {"tasks": 0, "payloads": 0}
    assert task_module._stored_image_data([legacy["data"][2]]) == [legacy["data"][2]]


def test_internal_result_retains_reference_but_public_output_never_exposes_it(task_env):
    result = conversation._format_image_bytes([IMAGE], "draw", "url")
    assert result == [{"url": URL, "_storage_reference": REFERENCE, "revised_prompt": "draw"}]
    output = conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1, data=result)
    public_data = [{"url": URL, "revised_prompt": "draw"}]
    assert output.to_chunk()["data"] == conversation.collect_image_outputs([output])["data"] == public_data
    assert conversation.collect_image_outputs([output], include_storage_reference=True)["data"] == result
    task_env.storage.save.assert_called_once_with(IMAGE, None)
    task_env.storage.save.reset_mock()
    data = conversation._format_image_bytes([IMAGE], "draw", "b64_json")
    assert data == [{"b64_json": base64.b64encode(IMAGE).decode("ascii"), "revised_prompt": "draw"}]
    task_env.storage.save.assert_not_called()
