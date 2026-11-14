"""Images response-format regressions; run through test/run_isolated.py."""
import json
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import ai
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
    save = Mock(return_value=IMAGE_URL)
    monkeypatch.setattr(conversation, "save_image_bytes", save)
    app = FastAPI()
    app.include_router(ai.create_router())
    payload = {"model": "gpt-image-2", "prompt": "draw", "stream": stream}
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
    if response_format == "url":
        save.assert_called_once()
    else:
        save.assert_not_called()
