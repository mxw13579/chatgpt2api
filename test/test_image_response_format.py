"""Images response-format regressions; run through test/run_isolated.py."""
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai
from services.image_storage_service import StoredImage
from services.protocol import conversation

PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lm2C6wAAAABJRU5ErkJggg=="
IMAGE_URL = "https://synthetic.invalid/image.png"


@pytest.mark.parametrize("path,handler", [
    ("/v1/images/generations", ai.openai_v1_image_generations),
    ("/v1/images/edits", ai.openai_v1_image_edit),
])
@pytest.mark.parametrize("response_format", [None, "b64_json", "url"])
@pytest.mark.parametrize("stream", [False, True])
def test_images_return_only_the_selected_format(monkeypatch, path, handler, response_format, stream):
    def generate(request):
        result = conversation.format_image_result(
            [{"b64_json": PNG_B64}], request.prompt, request.response_format, request.base_url,
        )
        yield conversation.ImageOutput(
            kind="result", model=request.model, index=1, total=1, data=result["data"],
        )

    monkeypatch.setattr(handler, "stream_image_outputs_with_pool", generate)
    monkeypatch.setattr(handler, "count_text_tokens", Mock(return_value=1))
    stored = StoredImage(
        rel="images/private-synthetic.png", url=IMAGE_URL, storage="r2", size=1,
        image_id="images/private-synthetic.png", account_id="private-synthetic-account",
        expires_at="2099-01-01T01:00:00+00:00",
    )
    save = Mock(return_value=stored)
    monkeypatch.setattr(conversation.image_storage_service, "save", save)
    app = FastAPI()
    app.include_router(ai.create_router())
    payload = {
        "model": "gpt-image-2", "prompt": "draw", "stream": stream,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if path == "/v1/images/edits":
        payload["image"] = "data:image/png;base64," + PNG_B64

    with TestClient(app) as client:
        response = client.post(path, headers={"Authorization": "Bearer chatgpt2api"}, json=payload)
    assert response.status_code == 200, response.text
    if stream:
        events = [
            json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert len(events) == 1
        data = events[0]["data"]
    else:
        data = response.json()["data"]
    expected = {"url": IMAGE_URL} if response_format == "url" else {"b64_json": PNG_B64}
    assert data == [{**expected, "revised_prompt": "draw"}]
    for private in ("_storage_reference", "image_id", "account_id", "expires_at", stored.account_id, stored.expires_at):
        assert private not in response.text
    if response_format == "url":
        save.assert_called_once()
    else:
        save.assert_not_called()
