"""Synthetic HTTP regressions; run only in the coordinator's isolated copy.

Importing api.ai imports service singletons, so configuration/data isolation and
an external network guard must already be in place before test discovery. These
fixtures never import api.app/main, start a server, or choose a real account.
All patches are test-local; no sys.modules or tokenizer state is replaced.
"""
from __future__ import annotations

import base64
import json
import struct
import unittest
import zlib
from contextlib import ExitStack
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import ai, support
from services.log_service import log_service
from services.protocol import chat_completion_cache as cache_module
from services.protocol import conversation
from services.protocol import openai_v1_chat_complete as chat
from services.protocol import openai_v1_response as responses
from services.protocol.conversation import ImageGenerationError, ImageOutput


CHAT_PATH = "/v1/chat/completions"
RESPONSES_PATH = "/v1/responses"
PROTOCOLS = {CHAT_PATH: chat, RESPONSES_PATH: responses}
MODEL = "gpt-image-2"
DEFAULT_IMAGE_MODEL = "gpt-image-2.5-sunburst"
TEXT_MODEL = "gpt-5-5"
PROMPT = "Draw a synthetic blue square."
INSTRUCTIONS = "Use the synthetic fixture style."
TEXT_REPLY = "Synthetic text reply."


def synthetic_png(rgb: tuple[int, int, int]) -> bytes:
    """A real, CRC-correct 1x1 RGB PNG, generated without image dependencies."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data)) + kind + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
        + chunk(b"IEND", b"")
    )


PNG_BYTES = (synthetic_png((255, 0, 0)), synthetic_png((0, 0, 255)))
PNG_BASE64 = tuple(base64.b64encode(data).decode("ascii") for data in PNG_BYTES)
DATA_URIS = tuple("data:image/png;base64," + value for value in PNG_BASE64)


def image_result(*images: str, total: int = 1) -> ImageOutput:
    return ImageOutput(
        kind="result", model=MODEL, index=1, total=total, created=123,
        data=[{"b64_json": image, "revised_prompt": PROMPT} for image in images],
    )


def progress_output() -> ImageOutput:
    return ImageOutput(
        kind="progress", model=MODEL, index=1, total=1,
        text="Drawing the synthetic fixture.\n",
    )


def text_only_output() -> ImageOutput:
    return ImageOutput(
        kind="message", model=MODEL, index=1, total=1,
        text="Synthetic upstream text instead of an image.",
    )


def text_source(field: str, text: str = PROMPT) -> dict:
    if field == "messages":
        return {field: [{"role": "user", "content": text}]}
    return {field: text}


def reference_source(field: str, urls=DATA_URIS) -> dict:
    if field == "messages":
        return {field: [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            *[{"type": "image_url", "image_url": {"url": url}} for url in urls],
        ]}]}
    return {field: [{"role": "user", "content": [
        {"type": "input_text", "text": PROMPT},
        *[{"type": "input_image", "image_url": url} for url in urls],
    ]}]}


def source_fields(path: str) -> tuple[str, str]:
    return ("messages", "input") if path == CHAT_PATH else ("input", "messages")


def sse_data(response) -> list:
    """Keep [DONE] visible so failure tests cannot accidentally ignore it."""
    payloads = []
    for frame in response.text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
        )
        if data:
            payloads.append(data if data == "[DONE]" else json.loads(data))
    return payloads


class ImageCompatHTTPFixture(unittest.TestCase):
    def patch_object(self, obj, name: str, **kwargs):
        return self.patches.enter_context(mock.patch.object(obj, name, **kwargs))

    def setUp(self) -> None:
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        # Replace, rather than inspect or amend, ambient configuration.
        self.config_data = {
            "default_image_model": DEFAULT_IMAGE_MODEL,
            "image_models": [MODEL, DEFAULT_IMAGE_MODEL, TEXT_MODEL],
            "global_system_prompt": "",
        }
        self.patch_object(ai.config, "data", new=self.config_data)
        self.cache_settings = {
            "enabled": False, "ttl_seconds": 60, "max_entries": 16,
            "dedupe_inflight": True, "stream_cache": True,
            "normalize_messages": False, "drop_assistant_history": False,
            "drop_adjacent_duplicates": False,
        }
        self.patch_object(
            ai.config, "get_chat_completion_cache_settings", return_value=self.cache_settings,
        )
        self.patches.enter_context(mock.patch(
            "utils.helper.configured_web_image_models",
            return_value={MODEL, DEFAULT_IMAGE_MODEL, TEXT_MODEL},
        ))
        # Both protocols share a real, but fresh, cache; ambient entries survive.
        self.cache = cache_module.ChatCompletionCache()
        self.cache.clear()
        self.addCleanup(self.cache.clear)
        self.patch_object(cache_module, "chat_completion_cache", new=self.cache)
        self.identity = self.patch_object(ai, "require_identity", return_value={
            "id": "synthetic-key-id", "name": "Synthetic HTTP fixture", "role": "admin",
        })
        # Keep this a Mock: the filtering tests assert the actual inspected text.
        self.request_filter = self.patch_object(support, "check_request", return_value=None)
        self.log_add = self.patch_object(log_service, "add", return_value=None)
        self.patches.enter_context(mock.patch("utils.helper.logger"))
        self.remote_get = self.patches.enter_context(mock.patch(
            "utils.helper.requests.get", side_effect=AssertionError("Unexpected remote fetch"),
        ))

        # Fail closed even if a future adapter accidentally bypasses its stubs.
        self.account_guard = mock.Mock(spec_set=[])
        self.patch_object(conversation, "account_service", new=self.account_guard)
        self.backend_guard = self.patch_object(
            conversation, "OpenAIBackendAPI", side_effect=AssertionError("Unexpected real backend"),
        )
        self.outputs = [image_result(PNG_BASE64[0])]
        self.pools = {}
        self.backends = {}
        self.deltas = {}
        self.searches = {}
        for path, module in PROTOCOLS.items():
            self.patch_object(module, "chat_completion_cache", new=self.cache)
            self.pools[path] = self.patch_object(
                module, "stream_image_outputs_with_pool",
                side_effect=lambda _request: iter(self.outputs),
            )
            self.backends[path] = self.patch_object(module, "text_backend", return_value=object())
            self.deltas[path] = self.patch_object(
                module, "stream_text_deltas",
                side_effect=lambda *_args: iter(["Synthetic ", "text reply."]),
            )
            self.searches[path] = self.patch_object(
                module, "run_web_search", side_effect=AssertionError("Unexpected web search"),
            )
            # Patch aliases at their call sites, never tiktoken's global state.
            self.patch_object(module, "count_text_tokens", return_value=7)
            self.patch_object(module, "count_message_text_tokens", return_value=7)
            self.patch_object(module, "count_message_image_tokens", return_value=0)
            self.patch_object(
                module, "count_image_inputs_tokens",
                side_effect=lambda images, _model: 13 if images else 0,
            )
            self.patch_object(
                module, "count_image_output_items_tokens",
                side_effect=lambda items, _size, _quality: 11 * len(items),
            )
        self.collect_text = self.patch_object(chat, "collect_text", return_value=TEXT_REPLY)
        app = FastAPI()
        app.include_router(ai.create_router())
        # In-process ASGI only; no socket listener or application lifespan hooks.
        self.client = self.patches.enter_context(TestClient(app))

    def tearDown(self) -> None:
        self.assertEqual(self.account_guard.mock_calls, [])
        self.backend_guard.assert_not_called()
        self.remote_get.assert_not_called()
        for search in self.searches.values():
            search.assert_not_called()

    def reset_calls(self) -> None:
        for patched in (
            self.identity, self.request_filter, self.log_add, self.collect_text,
            *self.pools.values(), *self.backends.values(), *self.deltas.values(),
        ):
            patched.reset_mock()

    def assert_no_text_backend(self) -> None:
        self.collect_text.assert_not_called()
        for patched in (*self.backends.values(), *self.deltas.values()):
            patched.assert_not_called()

    def assert_no_pool(self) -> None:
        for pool in self.pools.values():
            pool.assert_not_called()

    def pool_request(self, path: str):
        self.pools[path].assert_called_once()
        for other_path, pool in self.pools.items():
            if other_path != path:
                pool.assert_not_called()
        request = self.pools[path].call_args.args[0]
        self.assertIsInstance(request, conversation.ConversationRequest)
        self.assertEqual(request.response_format, "b64_json")
        self.assertFalse(request.message_as_error)
        self.assert_no_text_backend()
        return request

    def assert_image_success(
        self, response, path: str, *, images=PNG_BASE64[:1], model: str = MODEL, stream: bool = False,
    ) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        if stream:
            self.assertIn("text/event-stream", response.headers["content-type"])
            events = [item for item in sse_data(response) if isinstance(item, dict)]
            self.assertTrue(events)
            self.assertFalse(any("error" in event for event in events))
            if path == CHAT_PATH:
                self.assertTrue(all(event.get("object") == "chat.completion.chunk" for event in events))
                self.assertTrue(all(event["model"] == model for event in events))
                self.assertEqual(len({event["id"] for event in events}), 1)
                reasons = [event["choices"][0]["finish_reason"] for event in events]
                self.assertEqual(reasons.count("stop"), 1)
                self.assertEqual(reasons[-1], "stop")
                content = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
                for image in images:
                    self.assertIn(f"](data:image/png;base64,{image})", content)
                return
            self.assertEqual(events[0]["type"], "response.created")
            self.assertEqual(events[-1]["type"], "response.completed")
            self.assertEqual(sum(event["type"] == "response.completed" for event in events), 1)
            self.assertNotIn("response.failed", [event["type"] for event in events])
            self.assertEqual(events[0]["response"]["id"], events[-1]["response"]["id"])
            payload = events[-1]["response"]
        else:
            self.assertIn("application/json", response.headers["content-type"])
            payload = response.json()
        self.assertEqual(payload["model"], model)
        if path == CHAT_PATH:
            self.assertEqual(payload["object"], "chat.completion")
            self.assertNotIn("output", payload)
            choice = payload["choices"][0]
            self.assertEqual(choice["finish_reason"], "stop")
            self.assertEqual(choice["message"]["role"], "assistant")
            for image in images:
                self.assertIn(f"](data:image/png;base64,{image})", choice["message"]["content"])
        else:
            self.assertEqual(payload["object"], "response")
            self.assertEqual(payload["status"], "completed")
            self.assertIsNone(payload["error"])
            self.assertNotIn("choices", payload)
            self.assertEqual([item["type"] for item in payload["output"]], ["image_generation_call"] * len(images))
            self.assertEqual([item["status"] for item in payload["output"]], ["completed"] * len(images))
            self.assertEqual([item["result"] for item in payload["output"]], list(images))

    def assert_failed_sse(self, response, path: str) -> list[dict]:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])
        payloads = sse_data(response)
        self.assertTrue(payloads)
        self.assertNotIn("[DONE]", payloads)
        events = [item for item in payloads if isinstance(item, dict)]
        self.assertNotIn("response.completed", [event.get("type") for event in events])
        for event in events:
            self.assertNotEqual(event.get("response", {}).get("status"), "completed")
            for choice in event.get("choices", []):
                self.assertNotEqual(choice.get("finish_reason"), "stop")
        if path == RESPONSES_PATH:
            self.assertEqual(events[-1].get("type"), "response.failed")
            self.assertEqual(events[-1]["response"]["status"], "failed")
            self.assertTrue(events[-1]["response"]["error"])
        else:
            self.assertTrue(any(event.get("error") or event.get("type") == "error" for event in events))
        return events

    def assert_text_success(self, response, path: str, *, stream: bool) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        if stream:
            events = [item for item in sse_data(response) if isinstance(item, dict)]
            self.assertTrue(events)
            if path == CHAT_PATH:
                self.assertTrue(all(event.get("object") == "chat.completion.chunk" for event in events))
                self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")
                text = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
                self.assertEqual(text, TEXT_REPLY)
                return
            self.assertTrue(all(str(event.get("type", "")).startswith("response.") for event in events))
            self.assertEqual(events[0]["type"], "response.created")
            self.assertEqual(events[-1]["type"], "response.completed")
            payload = events[-1]["response"]
        else:
            payload = response.json()
        self.assertEqual(payload["model"], TEXT_MODEL)
        if path == CHAT_PATH:
            self.assertEqual(payload["object"], "chat.completion")
            self.assertEqual(payload["choices"][0]["message"]["content"], TEXT_REPLY)
        else:
            self.assertEqual(payload["object"], "response")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["output"][0]["type"], "message")
            self.assertEqual(payload["output"][0]["content"][0], {
                "type": "output_text", "text": TEXT_REPLY, "annotations": [],
            })


class ImageRoutingHTTPTests(ImageCompatHTTPFixture):
    def test_explicit_image_model_accepts_prompt_messages_and_input_without_tools(self) -> None:
        for path in PROTOCOLS:
            for field in ("prompt", "messages", "input"):
                for stream in (False, True):
                    with self.subTest(path=path, field=field, stream=stream):
                        self.reset_calls()
                        response = self.client.post(path, json={
                            "model": MODEL, "stream": stream, **text_source(field),
                        })
                        self.assert_image_success(response, path, stream=stream)
                        request = self.pool_request(path)
                        self.assertEqual((request.model, request.prompt, request.n), (MODEL, PROMPT, 1))
                        self.assertIsNone(request.images)
                        self.identity.assert_called_once_with(None)

    def test_n_size_quality_and_both_reference_images_reach_pool_for_either_field(self) -> None:
        self.outputs = [image_result(*PNG_BASE64, total=2)]
        for path in PROTOCOLS:
            for field in ("messages", "input"):
                for stream in (False, True):
                    with self.subTest(path=path, field=field, stream=stream):
                        self.reset_calls()
                        response = self.client.post(path, json={
                            "model": MODEL, "stream": stream, "n": 2,
                            "size": "1536x1024", "quality": "high", "response_format": "url",
                            **reference_source(field),
                        })
                        self.assert_image_success(response, path, images=PNG_BASE64, stream=stream)
                        request = self.pool_request(path)
                        self.assertEqual((request.model, request.prompt), (MODEL, PROMPT))
                        self.assertEqual((request.n, request.size, request.quality), (2, "1536x1024", "high"))
                        self.assertEqual(request.images, list(PNG_BASE64))

    def test_auto_with_image_tool_uses_configured_default_not_auto(self) -> None:
        for path in PROTOCOLS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.reset_calls()
                    native, _fallback = source_fields(path)
                    response = self.client.post(path, json={
                        "model": "auto", "stream": stream,
                        "tools": [{"type": "image_generation"}], **text_source(native),
                    })
                    self.assert_image_success(response, path, model=DEFAULT_IMAGE_MODEL, stream=stream)
                    self.assertEqual(self.pool_request(path).model, DEFAULT_IMAGE_MODEL)

    def test_gpt_5_5_without_tools_keeps_text_backend_in_both_http_modes(self) -> None:
        for path in PROTOCOLS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.reset_calls()
                    native, _fallback = source_fields(path)
                    response = self.client.post(path, json={
                        "model": TEXT_MODEL, "stream": stream, **text_source(native),
                    })
                    self.assert_text_success(response, path, stream=stream)
                    self.assert_no_pool()
                    self.backends[path].assert_called_once_with(TEXT_MODEL)
                    generate = self.collect_text if path == CHAT_PATH and not stream else self.deltas[path]
                    generate.assert_called_once()
                    backend, request = generate.call_args.args
                    self.assertIs(backend, self.backends[path].return_value)
                    self.assertEqual(request.model, TEXT_MODEL)
                    self.assertEqual(request.messages, [{"role": "user", "content": PROMPT}])
                    other_path = RESPONSES_PATH if path == CHAT_PATH else CHAT_PATH
                    self.backends[other_path].assert_not_called()
                    self.deltas[other_path].assert_not_called()

    def test_remote_reference_uses_mocked_get_and_preserves_inline_reference(self) -> None:
        remote_url = "https://synthetic.invalid/reference.png"
        for path in PROTOCOLS:
            for field in ("messages", "input"):
                with self.subTest(path=path, field=field):
                    self.reset_calls()
                    remote_response = mock.Mock(
                        status_code=200, content=PNG_BYTES[0], url=remote_url,
                        headers={"content-type": "image/png", "Content-Type": "image/png"},
                    )
                    remote_response.raise_for_status.return_value = None
                    remote_response.iter_content.side_effect = lambda *_a, **_kw: iter([PNG_BYTES[0]])
                    # This replaces the fail-closed GET guard only within this request.
                    with mock.patch("utils.helper.requests.get", return_value=remote_response) as get:
                        response = self.client.post(path, json={
                            "model": MODEL,
                            **reference_source(field, (remote_url, DATA_URIS[1])),
                        })
                    self.assert_image_success(response, path)
                    get.assert_called()
                    for call in get.call_args_list:
                        self.assertEqual(call.args[0] if call.args else call.kwargs["url"], remote_url)
                    self.assertEqual(self.pool_request(path).images, list(PNG_BASE64))


class ImageFailureHTTPTests(ImageCompatHTTPFixture):
    def test_tool_choice_none_conflict_is_http_400_before_pool(self) -> None:
        for path in PROTOCOLS:
            with self.subTest(path=path):
                self.reset_calls()
                response = self.client.post(path, json={
                    "model": MODEL, "prompt": PROMPT, "tool_choice": "none", "stream": False,
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_pool()
                self.assert_no_text_backend()

    def test_zero_boolean_and_fractional_n_are_rejected_without_coercion(self) -> None:
        # Chat's API model must use StrictInt; bool must not silently become 1.
        for path in PROTOCOLS:
            for count in (0, True, 1.5):
                with self.subTest(path=path, n=count):
                    self.reset_calls()
                    response = self.client.post(path, json={
                        "model": MODEL, "prompt": PROMPT, "n": count, "stream": False,
                    })
                    self.assertIn(response.status_code, (400, 422), response.text)
                    self.assert_no_pool()
                    self.assert_no_text_backend()

    def test_stream_prevalidation_can_be_http_400_or_typed_failure_but_never_complete(self) -> None:
        for path in PROTOCOLS:
            for invalid in ({"tool_choice": "none"}, {"n": 0}):
                with self.subTest(path=path, invalid=invalid):
                    self.reset_calls()
                    response = self.client.post(path, json={
                        "model": MODEL, "prompt": PROMPT, "stream": True, **invalid,
                    })
                    if response.status_code != 400:
                        self.assert_failed_sse(response, path)
                    self.assert_no_pool()
                    self.assert_no_text_backend()

    def test_missing_text_only_and_short_image_results_fail_closed_over_http(self) -> None:
        cases = [
            ("empty", [], 1, "no_image_generated"),
            ("empty_result", [image_result()], 1, "no_image_generated"),
            ("progress_only", [progress_output()], 1, "no_image_generated"),
            ("short_count", [image_result(PNG_BASE64[0], total=2)], 2, "incomplete_image_generation"),
        ]
        for path in PROTOCOLS:
            for stream in (False, True):
                for label, outputs, count, code in cases:
                    with self.subTest(path=path, stream=stream, case=label):
                        self.reset_calls()
                        self.outputs = outputs
                        response = self.client.post(path, json={
                            "model": MODEL, "prompt": PROMPT, "stream": stream, "n": count,
                        })
                        if not stream or (path == CHAT_PATH and response.status_code == 502):
                            self.assertEqual(response.status_code, 502, response.text)
                            self.assertEqual(response.json()["error"]["code"], code)
                            self.assertNotIn("choices", response.json())
                            self.assertNotIn("output", response.json())
                        else:
                            events = self.assert_failed_sse(response, path)
                            if path == RESPONSES_PATH:
                                self.assertEqual(events[0]["type"], "response.created")
                                self.assertEqual(events[-1]["response"]["error"]["code"], code)
                                self.assertEqual(events[0]["response"]["id"], events[-1]["response"]["id"])
                        self.pool_request(path)

    def test_parallel_task_failure_cannot_be_hidden_by_extra_images_from_another_task(self) -> None:
        self.config_data["image_parallel_generation"] = True

        def generate(request, index, total):
            self.assertTrue(request.strict_image_results)
            if index == 1:
                return [image_result(*PNG_BASE64, total=2)]
            raise ImageGenerationError("Synthetic second-task failure.")

        with mock.patch.object(conversation, "_generate_single_image", side_effect=generate):
            for path in PROTOCOLS:
                with self.subTest(path=path):
                    self.reset_calls()
                    self.pools[path].side_effect = conversation.stream_image_outputs_with_pool
                    response = self.client.post(path, json={"model": MODEL, "prompt": PROMPT, "n": 2})
                    self.assertEqual(response.status_code, 502, response.text)
                    self.assertEqual(response.json()["error"]["code"], "incomplete_image_generation")

    def test_failure_after_an_image_does_not_emit_stop_or_response_completed(self) -> None:
        def outputs_then_error(_request):
            yield image_result(PNG_BASE64[0])
            raise ImageGenerationError("Synthetic delayed failure.")

        for path in PROTOCOLS:
            with self.subTest(path=path):
                self.reset_calls()
                self.pools[path].side_effect = outputs_then_error
                response = self.client.post(path, json={
                    "model": MODEL, "prompt": PROMPT, "stream": True,
                })
                self.assert_failed_sse(response, path)
                self.pool_request(path)


class ImageFilteringAndLoggingHTTPTests(ImageCompatHTTPFixture):
    def test_filter_receives_selected_prompt_without_instructions_including_other_protocol_fields(self) -> None:
        for path in PROTOCOLS:
            native, fallback = source_fields(path)
            native_text = "Synthetic native prompt."
            ignored = "Synthetic unselected prompt."
            empty_fallback = [] if fallback == "messages" else ""
            cases = [
                ("missing_native", text_source(fallback), PROMPT),
                ("null_native", {native: None, **text_source(fallback)}, PROMPT),
                ("empty_fallback", {fallback: empty_fallback, **text_source(native)}, PROMPT),
                ("native_wins", {**text_source(native, native_text), **text_source(fallback, ignored)}, native_text),
                ("direct_wins", {
                    "prompt": "  " + PROMPT + "  ",
                    **text_source(native, native_text), **text_source(fallback, ignored),
                }, PROMPT),
                ("direct_with_empty_sources", {"prompt": PROMPT, native: None, fallback: empty_fallback}, PROMPT),
                ("blank_direct", {"prompt": "  ", **text_source(native, native_text)}, native_text),
                ("only_user_turns", {native: [
                    {"role": "system", "content": "Synthetic unselected system text."},
                    {"role": "user", "content": "Synthetic first user turn."},
                    {"role": "assistant", "content": "Synthetic unselected assistant text."},
                    {"role": "user", "content": [{"type": "input_text", "text": "Synthetic second user turn."}]},
                ]}, "Synthetic second user turn."),
            ]
            for label, source, selected in cases:
                with self.subTest(path=path, case=label):
                    self.reset_calls()
                    response = self.client.post(path, json={
                        "model": MODEL, "instructions": "  " + INSTRUCTIONS + "  ", **source,
                    })
                    self.assert_image_success(response, path)
                    expected = selected
                    self.request_filter.assert_called_once_with(expected)
                    self.assertEqual(self.pool_request(path).prompt, expected)

    def test_explicit_empty_native_field_does_not_select_nonempty_fallback(self) -> None:
        for path in PROTOCOLS:
            with self.subTest(path=path):
                self.reset_calls()
                native, fallback = source_fields(path)
                response = self.client.post(path, json={
                    "model": MODEL, native: [] if native == "messages" else "",
                    **text_source(fallback),
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_pool()
                self.assert_no_text_backend()

    def test_filter_403_on_cross_protocol_fields_prevents_any_backend_call(self) -> None:
        self.request_filter.side_effect = HTTPException(status_code=403, detail="Synthetic fixture blocked.")
        for path in PROTOCOLS:
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    self.reset_calls()
                    _native, fallback = source_fields(path)
                    with mock.patch.object(ai.LoggedCall, "log") as call_log:
                        response = self.client.post(path, json={
                            "model": MODEL, "stream": stream, "instructions": INSTRUCTIONS,
                            **text_source(fallback),
                        })
                    self.assertEqual(response.status_code, 403, response.text)
                    self.request_filter.assert_called_once_with(PROMPT)
                    self.assert_no_pool()
                    self.assert_no_text_backend()
                    call_log.assert_called_once()
                    self.assertEqual(call_log.call_args.kwargs["status"], "failed")

    def test_missing_image_prompt_is_logged_as_failure(self) -> None:
        for path in PROTOCOLS:
            with self.subTest(path=path):
                self.reset_calls()
                response = self.client.post(path, json={"model": MODEL})
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_no_pool()
                self.request_filter.assert_not_called()
                self.log_add.assert_called_once()
                self.assertEqual(self.log_add.call_args.args[2]["status"], "failed")

    def test_responses_failure_after_created_records_failed_not_success(self) -> None:
        # Keep LoggedCall.log real here: only the persistent log sink is mocked.
        self.outputs = []
        response = self.client.post(RESPONSES_PATH, json={
            "model": MODEL, "input": PROMPT, "stream": True,
        })
        events = self.assert_failed_sse(response, RESPONSES_PATH)
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[-1]["type"], "response.failed")
        self.pool_request(RESPONSES_PATH)
        self.log_add.assert_called()
        details = [
            call.args[2] if len(call.args) > 2 else call.kwargs["detail"]
            for call in self.log_add.call_args_list
        ]
        self.assertTrue(all(detail["endpoint"] == RESPONSES_PATH for detail in details))
        self.assertEqual(details[-1]["status"], "failed")
        self.assertNotIn("success", [detail["status"] for detail in details])


class CrossProtocolTextCacheHTTPTests(ImageCompatHTTPFixture):
    def test_identical_text_streams_do_not_replay_other_protocol_sse(self) -> None:
        self.cache_settings["enabled"] = True
        bodies = {
            CHAT_PATH: {
                "model": TEXT_MODEL, "stream": True, "tools": None, "tool_choice": None,
                "messages": [{"role": "user", "content": PROMPT}],
            },
            RESPONSES_PATH: {
                "model": TEXT_MODEL, "stream": True, "tools": None, "tool_choice": None,
                "input": PROMPT,
            },
        }
        # Chat first reproduces the old collision: tools/tool_choice must be
        # explicitly null on Chat to match Responses' API-model defaults.
        for order in ((CHAT_PATH, RESPONSES_PATH), (RESPONSES_PATH, CHAT_PATH)):
            with self.subTest(first=order[0]):
                self.cache.clear()
                self.reset_calls()
                with (
                    mock.patch.object(chat, "cache_key", wraps=cache_module.cache_key) as chat_key,
                    mock.patch.object(responses, "cache_key", wraps=cache_module.cache_key) as responses_key,
                ):
                    first_results = {}
                    for path in order:
                        response = self.client.post(path, json=bodies[path])
                        self.assert_text_success(response, path, stream=True)
                        first_results[path] = sse_data(response)
                    # Prove the canonical request digests collide, so the test
                    # exercises protocol separation rather than different keys.
                    self.assertEqual(
                        cache_module.cache_key(*chat_key.call_args.args, **chat_key.call_args.kwargs),
                        cache_module.cache_key(*responses_key.call_args.args, **responses_key.call_args.kwargs),
                    )
                    for path in order:
                        response = self.client.post(path, json=bodies[path])
                        self.assert_text_success(response, path, stream=True)
                        self.assertEqual(sse_data(response), first_results[path])
                for path in PROTOCOLS:
                    self.backends[path].assert_called_once_with(TEXT_MODEL)
                    self.deltas[path].assert_called_once()
                self.collect_text.assert_not_called()
                self.assert_no_pool()


if __name__ == "__main__":
    unittest.main()
