"""Direct storage-reference contract tests; run only via test/run_isolated.py.

All payloads and receipts are synthetic. Storage and generation are mocked;
no application lifespan, real account, network or authentication is used.
"""
from __future__ import annotations

import base64
import copy
import json
from unittest.mock import Mock

import pytest

from services import image_task_service as task_module
from services.image_storage_service import ImageStorageError, StoredImage
from services.protocol import conversation, openai_v1_image_edit, openai_v1_image_generations

IMAGE = b"synthetic-reference-image"
B64 = base64.b64encode(IMAGE).decode("ascii")
URL = "https://removed.synthetic.invalid/image.png"
BASE_URL = "https://app.synthetic.invalid"
REFERENCE = {
    "image_id": "images/synthetic-receipt.png",
    "account_id": "synthetic-storage-account",
    "expires_at": "2099-01-01T01:00:00+00:00",
}
RECEIPT = StoredImage(rel=REFERENCE["image_id"], url=URL, storage="r2", size=len(IMAGE), **REFERENCE)


@pytest.mark.parametrize("source", ["bytes", "codex"])
@pytest.mark.parametrize("storage_type", ["r2", "local", "webdav", "both"])
def test_formatter_uses_one_receipt_and_only_r2_keeps_private_reference(monkeypatch, source, storage_type):
    receipt = StoredImage(
        rel=RECEIPT.rel, url=URL, storage=storage_type, size=len(IMAGE), **REFERENCE,
    )
    storage = Mock(spec=["save"])
    storage.save.return_value = receipt
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    if source == "bytes":
        data = conversation._format_image_bytes([IMAGE], " draw ", "url", BASE_URL)
    else:
        data = conversation.format_image_result([{"b64_json": B64}], " draw ", "url", BASE_URL)["data"]
    expected = {"url": URL, "revised_prompt": "draw"}
    if storage_type == "r2":
        expected["_storage_reference"] = REFERENCE
    assert data == [expected]
    storage.save.assert_called_once_with(IMAGE, BASE_URL)


@pytest.mark.parametrize("protocol", [openai_v1_image_generations, openai_v1_image_edit])
@pytest.mark.parametrize("response_format", ["url", "b64_json"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("include_reference", [False, True])
def test_only_internal_nonstream_keyword_can_retain_reference(
    monkeypatch, protocol, response_format, stream, include_reference,
):
    storage = Mock(spec=["save"])
    storage.save.return_value = RECEIPT
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    generated = []

    def outputs(request):
        data = conversation._format_image_bytes([IMAGE], request.prompt, request.response_format, request.base_url)
        generated.extend(copy.deepcopy(data))
        yield conversation.ImageOutput(kind="result", model=request.model, index=1, total=1, data=data)

    monkeypatch.setattr(protocol, "stream_image_outputs_with_pool", outputs)
    monkeypatch.setattr(protocol, "count_text_tokens", lambda *_args: 1)
    if protocol is openai_v1_image_edit:
        monkeypatch.setattr(protocol, "count_image_inputs_tokens", lambda *_args: 1)
    body = {
        "model": "gpt-image-2", "prompt": "draw", "response_format": response_format,
        "stream": stream, "base_url": BASE_URL, "include_storage_reference": True,
        "images": [(IMAGE, "synthetic.png", "image/png")],
    }
    # Public callers use the default even when an untrusted body asks for refs.
    result = protocol.handle(body, include_storage_reference=True) if include_reference else protocol.handle(body)
    if stream:
        chunks = list(result)
        assert len(chunks) == 1
        result = chunks[0]
    expected = {response_format: URL if response_format == "url" else B64, "revised_prompt": "draw"}
    if include_reference and not stream and response_format == "url":
        expected["_storage_reference"] = REFERENCE
    else:
        serialized = json.dumps(result)
        for private in ("_storage_reference", "image_id", "account_id", "expires_at", REFERENCE["account_id"]):
            assert private not in serialized
    assert result["data"] == [expected]
    if response_format == "url":
        storage.save.assert_called_once_with(IMAGE, BASE_URL)
        assert generated == [{"url": URL, "revised_prompt": "draw", "_storage_reference": REFERENCE}]
    else:
        storage.save.assert_not_called()
        assert generated == [expected]


def test_public_serialization_does_not_remove_task_reference_from_output():
    data = [{"url": URL, "revised_prompt": "draw", "_storage_reference": dict(REFERENCE)}]
    output = conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1, data=data)
    before = copy.deepcopy(output)
    public = [{"url": URL, "revised_prompt": "draw"}]
    assert output.to_chunk()["data"] == public
    assert conversation.collect_image_outputs([output])["data"] == public
    assert conversation.collect_image_outputs([output], include_storage_reference=True)["data"] == data
    assert output == before


def test_task_persists_direct_reference_with_or_without_obsolete_url_and_keeps_legacy_url(monkeypatch):
    # Persistence cannot query URL/domain/index state or silently downgrade an R2
    # result to its obsolete URL. The private receipt is already authoritative.
    monkeypatch.setattr(task_module, "image_storage_service", Mock(spec=[]))
    legacy = {"url": "https://legacy.synthetic.invalid/image.png", "revised_prompt": "legacy"}
    data = [
        {"url": URL, "_storage_reference": dict(REFERENCE), "revised_prompt": "draw"},
        {"_storage_reference": dict(REFERENCE)},
        legacy,
    ]
    before = copy.deepcopy(data)
    assert task_module._stored_image_data(data) == [
        {**REFERENCE, "revised_prompt": "draw"}, dict(REFERENCE), legacy,
    ]
    assert data == before


def test_invalid_private_reference_does_not_silently_fall_back_to_url(monkeypatch):
    monkeypatch.setattr(task_module, "image_storage_service", Mock(spec=[]))
    with pytest.raises(TypeError):
        task_module._stored_image_data([{"url": URL, "_storage_reference": None}])


def test_storage_failure_propagates_without_a_url_or_reference_fallback(monkeypatch):
    storage = Mock(spec=["save"])
    failure = ImageStorageError("synthetic unavailable storage")
    storage.save.side_effect = failure
    monkeypatch.setattr(conversation, "image_storage_service", storage)
    with pytest.raises(ImageStorageError) as caught:
        conversation._format_image_bytes([IMAGE], "draw", "url", BASE_URL)
    assert caught.value is failure
    storage.save.assert_called_once_with(IMAGE, BASE_URL)
