"""Image result storage contract tests; run only via test/run_isolated.py.

All payloads and receipts are synthetic. Storage and generation are mocked;
no application lifespan, real account, network or authentication is used.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.image_storage_service import ImageStorageError
from services.protocol import conversation, openai_v1_image_edit, openai_v1_image_generations

IMAGE = b"synthetic-reference-image"
B64 = base64.b64encode(IMAGE).decode("ascii")
URL = "https://removed.synthetic.invalid/image.png"
BASE_URL = "https://app.synthetic.invalid"
RECEIPT = SimpleNamespace(url=URL)


@pytest.mark.parametrize("source", ["bytes", "codex"])
def test_formatter_saves_once_and_returns_only_the_url(monkeypatch, source):
    storage = Mock(spec=["save"])
    storage.save.return_value = RECEIPT
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    if source == "bytes":
        data = conversation._format_image_bytes([IMAGE], " draw ", "url", BASE_URL)
    else:
        data = conversation.format_image_result([{"b64_json": B64}], " draw ", "url", BASE_URL)["data"]
    assert data == [{"url": URL, "revised_prompt": "draw"}]
    storage.save.assert_called_once_with(IMAGE)


@pytest.mark.parametrize("protocol", [openai_v1_image_generations, openai_v1_image_edit])
@pytest.mark.parametrize("response_format", ["url", "b64_json"])
@pytest.mark.parametrize("stream", [False, True])
def test_handlers_store_only_url_results(monkeypatch, protocol, response_format, stream):
    storage = Mock(spec=["save"])
    storage.save.return_value = RECEIPT
    monkeypatch.setattr(conversation, "image_storage_service", storage)

    def outputs(request):
        data = conversation._format_image_bytes([IMAGE], request.prompt, request.response_format, request.base_url)
        yield conversation.ImageOutput(kind="result", model=request.model, index=1, total=1, data=data)

    monkeypatch.setattr(protocol, "stream_image_outputs_with_pool", outputs)
    monkeypatch.setattr(protocol, "count_text_tokens", lambda *_args: 1)
    if protocol is openai_v1_image_edit:
        monkeypatch.setattr(protocol, "count_image_inputs_tokens", lambda *_args: 1)
    body = {
        "model": "gpt-image-2", "prompt": "draw", "response_format": response_format,
        "stream": stream, "base_url": BASE_URL,
        "images": [(IMAGE, "synthetic.png", "image/png")],
    }
    result = protocol.handle(body)
    if stream:
        chunks = list(result)
        assert len(chunks) == 1
        result = chunks[0]
    assert result["data"] == [{response_format: URL if response_format == "url" else B64, "revised_prompt": "draw"}]
    if response_format == "url":
        storage.save.assert_called_once_with(IMAGE)
    else:
        storage.save.assert_not_called()


def test_storage_failure_propagates_without_a_url_fallback(monkeypatch):
    storage = Mock(spec=["save"])
    failure = ImageStorageError("synthetic unavailable storage")
    storage.save.side_effect = failure
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    with pytest.raises(ImageStorageError) as caught:
        conversation._format_image_bytes([IMAGE], "draw", "url", BASE_URL)
    assert caught.value is failure
    storage.save.assert_called_once_with(IMAGE)
