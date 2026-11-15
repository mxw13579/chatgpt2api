"""Synthetic service/pipeline regressions; run only via test/run_isolated.py.

No cloud, real application, authentication or production data is used.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from services import image_service
from services.image_storage_service import ImageStorageError
from services.protocol import conversation

REL = "images/2099/01/01/synthetic.png"
URL = f"https://first.synthetic.invalid/{REL}"


@pytest.fixture
def service_env(monkeypatch):
    settings = {"cleanup_interval_seconds": 60, "r2_accounts": [{"id": "synthetic-account"}]}
    storage = Mock(spec=["public_url", "cleanup_expired"])
    storage.public_url.return_value = URL
    monkeypatch.setattr(image_service, "config", SimpleNamespace(get_image_storage_settings=lambda: settings))
    monkeypatch.setattr(image_service, "image_storage_service", storage)
    monkeypatch.setattr(image_service, "logger", Mock())
    return SimpleNamespace(storage=storage, settings=settings)


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


def test_image_route_redirects_to_r2_public_url(service_env):
    response = image_service.get_image_response(REL)
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 307
    assert response.headers["location"] == URL
    assert response.headers["cache-control"] == "no-store"
    service_env.storage.public_url.assert_called_once_with(REL)


@pytest.mark.parametrize("status", [404, 410])
def test_image_route_propagates_unavailable_image(service_env, status):
    service_env.storage.public_url.side_effect = HTTPException(status, "synthetic-unavailable")
    with pytest.raises(HTTPException) as error:
        image_service.get_image_response(REL)
    assert error.value.status_code == status


def test_maintenance_uses_one_loop_and_dynamic_interval(service_env, monkeypatch):
    env = service_env
    env.settings["cleanup_interval_seconds"] = 15
    clock = [0.0]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 3

    def tick():
        env.settings["cleanup_interval_seconds"] = 30

    env.storage.cleanup_expired.side_effect = tick
    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [15, 30, 30]
    assert env.storage.cleanup_expired.call_count == 2


def test_maintenance_failure_is_sanitized_and_does_not_stop_the_loop(service_env, monkeypatch):
    env = service_env
    env.storage.cleanup_expired.side_effect = RuntimeError("synthetic-private-error")
    clock = [0.0]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 4

    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [60] * 4
    assert env.storage.cleanup_expired.call_count == 3
    assert image_service.logger.warning.call_count == 3
    assert "synthetic-private-error" not in str(image_service.logger.mock_calls)


def test_maintenance_without_accounts_does_not_call_r2(service_env, monkeypatch):
    env = service_env
    env.settings["r2_accounts"] = []
    clock = [0.0]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
        return len(waits) == 3

    monkeypatch.setattr(image_service.time, "monotonic", lambda: clock[0])
    image_service._auto_cleanup_worker(SimpleNamespace(wait=wait))
    assert waits == [60] * 3
    env.storage.cleanup_expired.assert_not_called()


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
