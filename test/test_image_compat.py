"""Pure adapter tests: synthetic images and stubbed service/config/network imports.

No application bootstrap, account files, image storage, or upstream calls are used.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
PNG_BYTES = b"synthetic-png"
JPEG_BYTES = b"synthetic-jpeg"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_URL = "data:image/png;base64," + PNG_B64
JPEG_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode("ascii")


def module_stub(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_module(name: str, path: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    parent, _, child = name.rpartition(".")
    setattr(sys.modules[parent], child, module)
    spec.loader.exec_module(module)
    return module


class ImageCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.forbidden_call = mock.Mock(side_effect=AssertionError("external service access forbidden"))
        self.config = types.SimpleNamespace(
            default_image_model="gpt-image-2",
            image_models=[
                "gpt-image-2", "gpt-image-2.5-sunburst", "chatgpt-image-fixture",
                "gpt-5-5", "gpt-5-5-thinking", "gpt-5-3", "fixture-canvas",
            ],
        )
        modules = {
            name: module_stub(name, __path__=[])
            for name in ("services", "services.protocol", "utils", "curl_cffi", "curl_cffi.requests")
        }
        modules.update({
            "services.config": module_stub("services.config", config=self.config),
            "services.proxy_service": module_stub("services.proxy_service", proxy_settings=self.forbidden_call),
            "services.account_service": module_stub("services.account_service", account_service=self.forbidden_call),
            "services.image_storage_service": module_stub("services.image_storage_service", image_storage_service=self.forbidden_call),
            "services.openai_backend_api": module_stub(
                "services.openai_backend_api",
                OpenAIBackendAPI=self.forbidden_call,
                ImageContentPolicyError=type("ImageContentPolicyError", (RuntimeError,), {}),
                ImagePollTimeoutError=type("ImagePollTimeoutError", (RuntimeError,), {}),
                ImageStreamHardTimeoutError=type("ImageStreamHardTimeoutError", (RuntimeError,), {}),
                TEXT_STREAM_TIMEOUT_SECS=1,
            ),
            "utils.log": module_stub("utils.log", logger=mock.Mock()),
            "utils.image_tokens": module_stub("utils.image_tokens", count_image_content_tokens=self.forbidden_call),
            "utils.ui_markup": module_stub("utils.ui_markup", strip_ui_markup=self.forbidden_call),
            "tiktoken": module_stub("tiktoken"),
            "curl_cffi.requests.exceptions": module_stub("curl_cffi.requests.exceptions", RequestException=RuntimeError),
            "curl_cffi.requests.models": module_stub("curl_cffi.requests.models", STREAM_END=object()),
        })
        modules["curl_cffi.requests"].Response = object
        modules["curl_cffi.requests"].get = self.forbidden_call
        for name, module in modules.items():
            parent, _, child = name.rpartition(".")
            if parent in modules:
                setattr(modules[parent], child, module)
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.helper = load_module("utils.helper", "utils/helper.py")
        self.conversation = load_module("services.protocol.conversation", "services/protocol/conversation.py")
        self.compat = load_module("services.protocol.image_compat", "services/protocol/image_compat.py")

    def tearDown(self) -> None:
        self.assertEqual(self.forbidden_call.mock_calls, [])

    def normalize(self, body: dict, protocol: str = "chat"):
        return self.compat.normalize_image_request(body, protocol=protocol)

    def assert_bad_request(self, body: dict, contains: str, protocol: str = "chat") -> None:
        with self.assertRaises(HTTPException) as caught:
            self.normalize(body, protocol)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn(contains, str(caught.exception.detail))

    def output(self, kind: str = "result", **kwargs):
        return self.conversation.ImageOutput(kind=kind, model="gpt-image-2", index=1, total=1, **kwargs)

    def assert_output_error(self, outputs, code: str, expected_count: int = 1) -> None:
        with self.assertRaises(self.conversation.ImageGenerationError) as caught:
            list(self.compat.require_image_outputs(outputs, expected_count=expected_count))
        self.assertEqual(caught.exception.code, code)

    def test_image_only_model_names_route_without_tools(self) -> None:
        for model in (
            "gpt-image-2", " GPT-IMAGE-FUTURE ", "chatgpt-image-fixture",
            "codex-gpt-image-2", "plus-codex-gpt-image-2",
            "team-codex-gpt-image-2", "pro-codex-gpt-image-2",
        ):
            with self.subTest(model=model):
                self.assertTrue(self.helper.is_explicit_image_model(model))
                self.assertTrue(self.compat.is_image_request({"model": model}))
                self.assertTrue(self.helper.is_image_chat_request({"model": model}))

    def test_capable_text_models_do_not_route_by_model_alone(self) -> None:
        for model in ("gpt-5-5", "gpt-5-5-thinking", "gpt-5-3", "fixture-canvas"):
            with self.subTest(model=model):
                self.assertTrue(self.helper.is_supported_image_model(model))
                self.assertFalse(self.helper.is_explicit_image_model(model))
                self.assertFalse(self.compat.is_image_request({"model": model}))
        for model in ("auto", "gpt-text-fixture", "plus-gpt-image-2", "free-codex-gpt-image-2", ""):
            self.assertFalse(self.compat.is_image_request({"model": model}))

    def test_explicit_tool_choice_tools_and_modalities_route(self) -> None:
        for intent in (
            {"tools": [{"type": "function", "name": "other"}, {"type": "image_generation"}]},
            {"tool_choice": {"type": "image_generation"}},
            {"modalities": ["text", " IMAGE "]},
        ):
            body = {"model": "gpt-5-5", **intent}
            self.assertTrue(self.compat.is_image_request(body))
            self.assertTrue(self.helper.is_image_chat_request(body))
        self.assertFalse(self.compat.is_image_request({"modalities": "image"}))
        self.assertFalse(self.compat.is_image_request({"tools": [{"type": "function", "name": "image_generation"}]}))

    def test_routing_reuses_helper(self) -> None:
        with mock.patch.object(self.compat, "is_image_chat_request", return_value=True) as route:
            body = {"model": "fixture"}
            self.assertTrue(self.compat.is_image_request(body))
            route.assert_called_once_with(body)

    def test_explicit_model_is_validated_not_silently_replaced(self) -> None:
        body = {"model": "gpt-image-not-configured", "prompt": "draw"}
        self.assertTrue(self.compat.is_image_request(body))
        self.assert_bad_request(body, "unsupported image model")
        for model in ("gpt-image-2", "chatgpt-image-fixture", "plus-codex-gpt-image-2", "team-codex-gpt-image-2", "pro-codex-gpt-image-2"):
            self.assertEqual(self.normalize({"model": model, "prompt": "draw"}).request.model, model)

    def test_image_model_and_tool_model_must_agree(self) -> None:
        body = {"model": "gpt-image-2", "prompt": "draw", "tools": [{"type": "image_generation", "model": "codex-gpt-image-2"}]}
        self.assert_bad_request(body, "conflicts")
        body["tools"][0]["model"] = " GPT-IMAGE-2 "
        self.assertEqual(self.normalize(body).request.model, "gpt-image-2")

    def test_tool_model_overrides_text_model_when_supported(self) -> None:
        for model in ("gpt-5-5", "auto", "unsupported-text"):
            result = self.normalize({"model": model, "prompt": "draw", "tools": [{"type": "image_generation", "model": "codex-gpt-image-2"}]})
            self.assertEqual(result.request.model, "codex-gpt-image-2")

    def test_supported_text_model_is_preserved_with_explicit_intent(self) -> None:
        for intent in ({"tools": [{"type": "image_generation"}]}, {"modalities": ["image"]}):
            for model in ("gpt-5-5", "gpt-5-3", "fixture-canvas"):
                self.assertEqual(self.normalize({"model": model, "prompt": "draw", **intent}).request.model, model)
        for empty_model in (None, "", "  "):
            result = self.normalize({"model": "gpt-5-5", "prompt": "draw", "tools": [{"type": "image_generation", "model": empty_model}]})
            self.assertEqual(result.request.model, "gpt-5-5")

    def test_unsupported_tool_model_is_rejected_not_silently_replaced(self) -> None:
        for model in (None, "", "auto", "unsupported-text", "gpt-5-5", "gpt-image-2"):
            for tool_model in ("unsupported", " GPT-IMAGE-NOT-CONFIGURED ", "free-codex-gpt-image-2"):
                with self.subTest(model=model, tool_model=tool_model):
                    self.assert_bad_request({
                        "model": model, "prompt": "draw",
                        "tools": [{"type": "image_generation", "model": tool_model}],
                    }, "unsupported image_generation tool.model")

    def test_auto_and_unsupported_text_models_use_config_default(self) -> None:
        self.config.default_image_model = "fixture-canvas"
        for model in (None, "", "auto", "unsupported-text"):
            for tool in (
                {"type": "image_generation"},
                {"type": "image_generation", "model": None},
                {"type": "image_generation", "model": ""},
                {"type": "image_generation", "model": "  "},
            ):
                result = self.normalize({"model": model, "prompt": "draw", "tools": [tool]})
                self.assertEqual(result.request.model, "fixture-canvas")

    def test_conflicting_tool_choice_is_not_silently_ignored(self) -> None:
        for choice in ("none", "web_search", {"type": "web_search"}, {"type": "function", "function": {"name": "other"}}):
            for intent in ({"model": "gpt-image-2"}, {"tools": [{"type": "image_generation"}]}, {"modalities": ["image"]}):
                body = {"prompt": "draw", "tool_choice": choice, **intent}
                self.assertTrue(self.compat.is_image_request(body))
                self.assert_bad_request(body, "tool_choice conflicts")

    def test_nonconflicting_tool_choices_are_allowed(self) -> None:
        for choice in (None, "auto", "required", {"type": "image_generation"}):
            self.assertEqual(self.normalize({"model": "gpt-image-2", "prompt": "draw", "tool_choice": choice}).request.prompt, "draw")

    def test_native_source_wins_and_sources_are_not_merged(self) -> None:
        body = {"model": "gpt-image-2", "messages": [{"role": "user", "content": "chat text"}], "input": "responses text"}
        self.assertEqual(self.normalize(body, "chat").request.prompt, "chat text")
        self.assertEqual(self.normalize(body, "responses").request.prompt, "responses text")

    def test_missing_or_null_native_source_falls_back(self) -> None:
        self.assertEqual(self.normalize({"model": "gpt-image-2", "input": "fallback"}).request.prompt, "fallback")
        self.assertEqual(self.normalize({"model": "gpt-image-2", "messages": [{"role": "user", "content": "fallback"}]}, "responses").request.prompt, "fallback")
        self.assertEqual(self.normalize({"model": "gpt-image-2", "messages": None, "input": "fallback"}).request.prompt, "fallback")

    def test_explicitly_empty_native_source_does_not_select_other_source(self) -> None:
        self.assert_bad_request({"model": "gpt-image-2", "messages": [], "input": "not selected"}, "prompt is required")
        self.assert_bad_request({"model": "gpt-image-2", "input": "", "messages": [{"role": "user", "content": "not selected"}]}, "prompt is required", "responses")

    def test_only_user_text_is_used_and_instructions_prefix_it(self) -> None:
        messages = [
            {"role": "system", "content": "system text"},
            {"role": "developer", "content": "developer text"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "assistant text"},
            {"role": "user", "content": [{"type": "input_text", "text": "second"}]},
            {"content": "missing role"},
        ]
        for protocol, field in (("chat", "messages"), ("responses", "input")):
            body = {"model": "gpt-image-2", field: messages, "instructions": "  style guide  "}
            self.assertEqual(self.normalize(body, protocol).request.prompt, "style guide\n\nfirst\nsecond")

    def test_direct_prompt_has_priority_but_keeps_selected_reference_images(self) -> None:
        body = {
            "model": "gpt-image-2", "prompt": " direct ", "instructions": "prefix",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "ignored"}, {"type": "image_url", "image_url": {"url": PNG_URL}}]}],
            "input": [{"type": "input_image", "file_id": "file-synthetic-unselected"}],
        }
        result = self.normalize(body)
        self.assertEqual(result.request.prompt, "prefix\n\ndirect")
        self.assertEqual(result.images, [(PNG_BYTES, "image_1.png", "image/png")])
        self.assertEqual(result.request.images, [PNG_B64])

    def test_empty_direct_prompt_falls_back_to_selected_user_text(self) -> None:
        self.assertEqual(self.normalize({"model": "gpt-image-2", "prompt": "  ", "input": "fallback"}).request.prompt, "fallback")

    def test_empty_prompt_cannot_be_replaced_by_instructions_or_assistant_text(self) -> None:
        for extra in ({}, {"instructions": "only instructions"}, {"messages": [{"role": "assistant", "content": "not user"}]}, {"input": "  "}):
            self.assert_bad_request({"model": "gpt-image-2", **extra}, "prompt is required")

    def test_prompt_and_instructions_reject_non_strings(self) -> None:
        self.assert_bad_request({"model": "gpt-image-2", "prompt": 12}, "prompt must be a string")
        self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", "instructions": {}}, "instructions must be a string")

    def test_prompt_preview_matches_normalized_prompt_for_both_protocols(self) -> None:
        self.assertIn("image_request_prompt", self.compat.__all__)
        for protocol, native, fallback in (("chat", "messages", "input"), ("responses", "input", "messages")):
            for fields, expected in (
                ({native: [{"role": "user", "content": "native"}], fallback: "ignored"}, "native"),
                ({fallback: [{"role": "user", "content": "fallback"}]}, "fallback"),
                ({native: None, fallback: "fallback"}, "fallback"),
                ({native: [], fallback: "ignored", "prompt": " direct "}, "direct"),
                ({native: "native", "prompt": "  "}, "native"),
                ({native: [{"role": "user", "content": "native"}], "prompt": "direct", "instructions": " prefix "}, "prefix\n\ndirect"),
                ({native: [
                    {"role": "system", "content": "ignored system"},
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "ignored assistant"},
                    {"role": "user", "content": [{"type": "input_text", "text": "second"}]},
                ], "instructions": " prefix "}, "prefix\n\nfirst\nsecond"),
                ({native: [{"type": "input_text", "input_text": "bare blocks"}]}, "bare blocks"),
            ):
                with self.subTest(protocol=protocol, expected=expected, fields=fields):
                    body = {"model": "gpt-image-2", **fields}
                    before = copy.deepcopy(body)
                    preview = self.compat.image_request_prompt(body, protocol=protocol)
                    self.assertEqual(preview, expected)
                    self.assertEqual(self.normalize(body, protocol).request.prompt, preview)
                    self.assertEqual(body, before)

    def test_prompt_preview_shares_prompt_validation(self) -> None:
        for protocol, native, fallback in (("chat", "messages", "input"), ("responses", "input", "messages")):
            for fields in (
                {}, {"instructions": "only instructions"},
                {"prompt": 12}, {"prompt": "draw", "instructions": {}},
                {native: [], fallback: "not selected"},
                {native: "", fallback: "not selected"},
                {native: [{"role": "assistant", "content": "not selected"}]},
            ):
                with self.subTest(protocol=protocol, fields=fields):
                    body = {"model": "gpt-image-2", **fields}
                    with self.assertRaises(HTTPException) as preview:
                        self.compat.image_request_prompt(body, protocol=protocol)
                    with self.assertRaises(HTTPException) as normalized:
                        self.normalize(body, protocol)
                    self.assertEqual(preview.exception.status_code, 400)
                    self.assertEqual(preview.exception.detail, normalized.exception.detail)

    def test_prompt_preview_never_decodes_or_downloads_image_references(self) -> None:
        images = [
            {"type": "input_image", "image_url": "https://synthetic.invalid/reference.png"},
            {"type": "input_image", "image_url": "data:image/png;base64,not-base64!"},
            {"type": "input_image", "file_id": "file-synthetic"},
            {"type": "input_image", "source": {"type": "base64", "data": "not-base64!"}},
            {"type": "image", "data": b"", "mime": "image/svg+xml"},
        ] * 3
        with (
            mock.patch.object(self.compat, "extract_image_from_message_content", self.forbidden_call),
            mock.patch.object(self.compat, "normalize_json_edit_images", self.forbidden_call),
        ):
            for protocol, field in (("chat", "messages"), ("responses", "input")):
                body = {
                    "model": "gpt-image-not-configured", "instructions": " prefix ",
                    "tools": [{"type": "image_generation", "model": "unsupported"}],
                    field: [{"role": "user", "content": [{"type": "text", "text": "edit"}, *images]}],
                }
                self.assertEqual(self.compat.image_request_prompt(body, protocol=protocol), "prefix\n\nedit")

    def test_normalization_reuses_exported_prompt_function(self) -> None:
        body = {"model": "gpt-image-2", "prompt": "original"}
        with mock.patch.object(self.compat, "image_request_prompt", return_value="shared prompt") as preview:
            self.assertEqual(self.normalize(body).request.prompt, "shared prompt")
        preview.assert_called_once_with(body, protocol="chat")

    def test_responses_content_array_collects_all_reference_images(self) -> None:
        result = self.normalize({"model": "gpt-image-2", "input": [
            {"type": "input_text", "text": "combine"},
            {"type": "input_image", "image_url": PNG_URL},
            {"type": "input_image", "image_url": JPEG_URL},
        ]}, "responses")
        self.assertEqual(result.request.prompt, "combine")
        self.assertEqual(result.images, [(PNG_BYTES, "image_1.png", "image/png"), (JPEG_BYTES, "image_2.jpg", "image/jpeg")])
        self.assertEqual(result.request.images, [PNG_B64, base64.b64encode(JPEG_BYTES).decode("ascii")])

    def test_latest_image_user_message_contributes_all_images(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "image_url", "image_url": JPEG_URL}]},
            {"role": "user", "content": [{"type": "image_url", "image_url": PNG_URL}, {"type": "image_url", "image_url": JPEG_URL}]},
            {"role": "assistant", "content": [{"type": "input_image", "file_id": "file-synthetic-assistant"}]},
            {"role": "user", "content": "combine recent references"},
        ]
        for protocol, field in (("chat", "messages"), ("responses", "input")):
            result = self.normalize({"model": "gpt-image-2", field: messages}, protocol)
            self.assertEqual([data for data, _, _ in result.images], [PNG_BYTES, JPEG_BYTES])

    def test_reference_images_never_leak_from_unselected_source(self) -> None:
        body = {"model": "gpt-image-2", "messages": [{"role": "user", "content": "plain chat"}], "input": [{"type": "input_image", "image_url": PNG_URL}]}
        self.assertEqual(self.normalize(body).images, [])
        self.assertIsNone(self.normalize(body).request.images)

    def test_file_id_references_fail_instead_of_becoming_text_only_generation(self) -> None:
        for image in (
            {"type": "input_image", "file_id": "file-synthetic"},
            {"type": "image_url", "image_url": {"file_id": "file-synthetic"}},
            {"type": "image", "source": {"file_id": "file-synthetic"}},
        ):
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "edit", "input": [image]}, "file_id", "responses")

    def test_ten_references_allowed_eleven_rejected(self) -> None:
        image = {"type": "input_image", "image_url": PNG_URL}
        body = {"model": "gpt-image-2", "prompt": "combine", "input": [image] * 10}
        self.assertEqual(len(self.normalize(body, "responses").images), 10)
        body["input"].append(image)
        self.assert_bad_request(body, "up to 10", "responses")

    def test_image_size_boundary_is_enforced_by_helper(self) -> None:
        with mock.patch.object(self.helper, "MAX_JSON_IMAGE_BYTES", len(PNG_BYTES)):
            body = {"model": "gpt-image-2", "prompt": "edit", "input": [{"type": "input_image", "image_url": PNG_URL}]}
            self.assertEqual(len(self.normalize(body, "responses").images), 1)
            body["input"][0]["image_url"] = "data:image/png;base64," + base64.b64encode(PNG_BYTES + b"x").decode("ascii")
            self.assert_bad_request(body, "too large", "responses")

    def test_invalid_mime_base64_and_empty_images_are_rejected(self) -> None:
        for url, error in (
            ("data:image/svg+xml;base64," + PNG_B64, "mime type"),
            ("data:image/png;base64,not-base64!", "invalid base64"),
            ("data:image/png;base64,", "empty"),
        ):
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "edit", "input": [{"type": "input_image", "image_url": url}]}, error, "responses")
        self.assert_bad_request({"model": "gpt-image-2", "prompt": "edit", "input": [{"type": "input_image"}]}, "include image data", "responses")

    def test_raw_base64_with_mime_and_filename_uses_helper_decoder(self) -> None:
        result = self.normalize({"model": "gpt-image-2", "prompt": "edit", "input": [{"type": "input_image", "b64_json": PNG_B64, "mime_type": "image/webp", "filename": "synthetic.webp"}]}, "responses")
        self.assertEqual(result.images, [(PNG_BYTES, "synthetic.webp", "image/webp")])

    def test_source_base64_bytes_and_plain_base64_keep_metadata(self) -> None:
        for part in (
            {"type": "input_image", "source": {"type": "base64", "data": PNG_B64, "media_type": "image/webp"}, "filename": "synthetic.webp"},
            {"type": "input_image", "source": {"type": "base64", "data": PNG_B64}, "mime_type": "image/webp", "filename": "synthetic.webp"},
            {"type": "image", "data": PNG_BYTES, "mime": "image/webp", "filename": "synthetic.webp"},
            {"type": "image", "data": bytearray(PNG_BYTES), "mime_type": "image/webp", "filename": "synthetic.webp"},
            {"type": "input_image", "base64": PNG_B64, "mime_type": "image/webp", "filename": "synthetic.webp"},
            {"type": "image_url", "image_url": {"url": PNG_B64, "mime_type": "image/webp", "filename": "synthetic.webp"}},
            {"type": "input_image", "image_url": {"url": PNG_B64, "mime_type": "image/webp", "filename": "synthetic.webp"}, "mime_type": " ", "filename": " "},
        ):
            for protocol, field in (("chat", "messages"), ("responses", "input")):
                with self.subTest(part=part, protocol=protocol):
                    body = {"model": "gpt-image-2", "prompt": "edit", field: [{"role": "user", "content": [part]}]}
                    before = copy.deepcopy(body)
                    result = self.normalize(body, protocol)
                    self.assertEqual(result.images, [(PNG_BYTES, "synthetic.webp", "image/webp")])
                    self.assertEqual(result.request.images, [PNG_B64])
                    self.assertEqual(body, before)

    def test_source_and_bytes_are_still_strictly_validated(self) -> None:
        for part, error in (
            ({"type": "input_image", "source": {"type": "base64", "data": "not-base64!", "media_type": "image/png"}}, "invalid base64"),
            ({"type": "input_image", "source": {"type": "base64", "data": PNG_B64, "media_type": "image/svg+xml"}}, "mime type"),
            ({"type": "image", "data": PNG_BYTES, "mime": "image/svg+xml"}, "mime type"),
            ({"type": "image", "data": b"", "mime": "image/png"}, "empty"),
        ):
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "edit", "input": [part]}, error, "responses")
        for part in (
            {"type": "input_image", "source": {"type": "base64", "data": PNG_B64, "media_type": "image/png"}},
            {"type": "image", "data": PNG_BYTES, "mime": "image/png"},
        ):
            with mock.patch.object(self.helper, "MAX_JSON_IMAGE_BYTES", len(PNG_BYTES) - 1):
                self.assert_bad_request({"model": "gpt-image-2", "prompt": "edit", "input": [part]}, "too large", "responses")

    def test_explicit_base64_fields_do_not_trigger_downloads(self) -> None:
        url = "https://synthetic.invalid/not-base64.png"
        for part in (
            {"type": "input_image", "b64_json": url},
            {"type": "input_image", "base64": url},
            {"type": "input_image", "source": {"type": "base64", "data": url, "media_type": "image/png"}},
        ):
            with mock.patch.object(self.compat, "extract_image_from_message_content", self.forbidden_call):
                with self.assertRaises(HTTPException) as caught:
                    self.normalize({"model": "gpt-image-2", "prompt": "edit", "input": [part]}, "responses")
            self.assertEqual(caught.exception.status_code, 400)

    def test_http_and_https_references_use_existing_extractor(self) -> None:
        for protocol, field in (("chat", "messages"), ("responses", "input")):
            for scheme in ("http", "https"):
                url = f"{scheme}://synthetic.invalid/reference.webp"
                for part in (
                    {"type": "image_url", "image_url": {"url": url, "filename": "synthetic.webp", "mime_type": "image/webp"}},
                    {"type": "input_image", "image_url": url, "filename": "synthetic.webp", "mime_type": "image/webp"},
                    {"type": "image", "url": url, "filename": "synthetic.webp", "mime_type": "image/webp"},
                ):
                    with self.subTest(protocol=protocol, part=part):
                        body = {"model": "gpt-image-2", "prompt": "edit", field: [{"role": "user", "content": [part]}]}
                        before = copy.deepcopy(body)
                        with mock.patch.object(self.compat, "extract_image_from_message_content", return_value=[(PNG_BYTES, "image/webp")]) as extractor:
                            result = self.normalize(body, protocol)
                        extractor.assert_called_once_with([{"type": "image_url", "image_url": url}])
                        self.assertEqual(result.images, [(PNG_BYTES, "synthetic.webp", "image/webp")])
                        self.assertEqual(result.request.images, [PNG_B64])
                        self.assertEqual(body, before)

    def test_remote_fetch_errors_propagate_unchanged(self) -> None:
        body = {"model": "gpt-image-2", "prompt": "edit", "input": [
            {"type": "input_image", "image_url": "https://synthetic.invalid/reference.png"},
        ]}
        for error in ("image_url fetch failed: HTTP 404", "image_url fetch failed: synthetic timeout", "image_url exceeds 10MB limit"):
            failure = HTTPException(status_code=400, detail={"error": error})
            with mock.patch.object(self.compat, "extract_image_from_message_content", side_effect=failure) as extractor:
                with self.assertRaises(HTTPException) as caught:
                    self.normalize(body, "responses")
            self.assertIs(caught.exception, failure)
            extractor.assert_called_once()

    def test_downloaded_images_are_validated_again_by_json_helper(self) -> None:
        body = {"model": "gpt-image-2", "prompt": "edit", "input": [
            {"type": "input_image", "image_url": "https://synthetic.invalid/reference.png", "mime_type": "image/png"},
        ]}
        for downloaded, error in (
            ([], "include image data"),
            ([(b"", "image/png")], "empty"),
            ([(PNG_BYTES, "image/svg+xml")], "mime type"),
        ):
            with mock.patch.object(self.compat, "extract_image_from_message_content", return_value=downloaded):
                self.assert_bad_request(body, error, "responses")
        with (
            mock.patch.object(self.compat, "extract_image_from_message_content", return_value=[(PNG_BYTES, "image/png")]),
            mock.patch.object(self.helper, "MAX_JSON_IMAGE_BYTES", len(PNG_BYTES) - 1),
        ):
            self.assert_bad_request(body, "too large", "responses")

    def test_remote_count_limit_is_checked_before_any_download(self) -> None:
        remote = {"type": "input_image", "image_url": "https://synthetic.invalid/reference.png"}
        local = {"type": "input_image", "image_url": PNG_URL}
        for protocol, field in (("chat", "messages"), ("responses", "input")):
            for images in ([remote] * 11, [remote, *([local] * 10)]):
                body = {"model": "gpt-image-2", "prompt": "edit", field: [{"role": "user", "content": images}]}
                with mock.patch.object(self.compat, "extract_image_from_message_content", side_effect=AssertionError("download before count validation")) as extractor:
                    self.assert_bad_request(body, "up to 10", protocol)
                extractor.assert_not_called()
            body[field][0]["content"] = [remote] * 10
            with mock.patch.object(self.compat, "extract_image_from_message_content", return_value=[(PNG_BYTES, "image/png")]) as extractor:
                result = self.normalize(body, protocol)
            self.assertEqual(extractor.call_count, 10)
            self.assertEqual(len(result.images), 10)
            self.assertEqual(result.images[-1], (PNG_BYTES, "image_10.png", "image/png"))

    def test_only_selected_latest_references_are_downloaded(self) -> None:
        remote = {"type": "input_image", "image_url": "https://synthetic.invalid/ignored.png"}
        selected = {"type": "input_image", "image_url": "https://synthetic.invalid/selected.png"}
        for protocol, native, fallback in (("chat", "messages", "input"), ("responses", "input", "messages")):
            body = {
                "model": "gpt-image-2", "prompt": "edit", fallback: [remote],
                native: [
                    {"role": "user", "content": [remote]},
                    {"role": "user", "content": [selected, {"type": "input_image", "image_url": JPEG_URL}]},
                    {"role": "assistant", "content": [remote]},
                    {"role": "user", "content": "edit the latest references"},
                ],
            }
            with mock.patch.object(self.compat, "extract_image_from_message_content", return_value=[(PNG_BYTES, "image/png")]) as extractor:
                result = self.normalize(body, protocol)
            extractor.assert_called_once_with([{"type": "image_url", "image_url": selected["image_url"]}])
            self.assertEqual(result.images, [(PNG_BYTES, "image_1.png", "image/png"), (JPEG_BYTES, "image_2.jpg", "image/jpeg")])

    def test_top_level_non_null_options_override_first_image_tool(self) -> None:
        body = {
            "model": "gpt-image-2", "prompt": "draw", "n": 2, "size": " 1024x1536 ", "quality": "high",
            "tools": [{"type": "function", "n": 4}, {"type": "image_generation", "n": 3, "size": "512x512", "quality": "low"}, {"type": "image_generation", "n": 4}],
        }
        request = self.normalize(body).request
        self.assertEqual((request.n, request.size, request.quality), (2, "1024x1536", "high"))
        body.update(n=None, size=None, quality=None)
        request = self.normalize(body).request
        self.assertEqual((request.n, request.size, request.quality), (3, "512x512", "low"))

    def test_option_defaults_and_response_shell_invariants(self) -> None:
        for response_format in (None, "url", {"type": "json_object"}):
            request = self.normalize({"model": "gpt-image-2", "prompt": "draw", "response_format": response_format}).request
            self.assertIsInstance(request, self.conversation.ConversationRequest)
            self.assertEqual((request.n, request.size, request.quality), (1, None, "auto"))
            self.assertEqual(request.response_format, "b64_json")
            self.assertTrue(request.message_as_error)

    def test_n_is_strict_integer_one_through_four(self) -> None:
        for value in (1, 2, 3, 4):
            self.assertEqual(self.normalize({"model": "gpt-image-2", "prompt": "draw", "n": value}).request.n, value)
        for value in (0, -1, 5, True, False, 1.0, 1.5, "2", "", [], {}):
            with self.subTest(value=value):
                self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", "n": value}, "n must")
                self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", "tools": [{"type": "image_generation", "n": value}]}, "n must")

    def test_invalid_top_level_options_do_not_fall_through_to_tool(self) -> None:
        for options, error in (({"n": 0}, "n must"), ({"size": " "}, "size must"), ({"size": 1024}, "size must"), ({"quality": False}, "quality must")):
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", "tools": [{"type": "image_generation", "n": 2, "size": "1024x1024", "quality": "high"}], **options}, error)
        self.assertEqual(self.normalize({"model": "gpt-image-2", "prompt": "draw", "quality": ""}).request.quality, "auto")

    def test_unsupported_output_encoding_and_previews_fail_explicitly(self) -> None:
        for option, error in (({"output_format": "jpeg"}, "only PNG"), ({"output_format": "webp"}, "only PNG"), ({"partial_images": 1}, "previews")):
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", **option}, error)
            self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", "tools": [{"type": "image_generation", **option}]}, error)
        self.normalize({"model": "gpt-image-2", "prompt": "draw", "output_format": "png", "partial_images": 0})

    def test_normalization_does_not_mutate_body(self) -> None:
        body = {"model": " GPT-IMAGE-2 ", "input": [{"type": "input_text", "text": "draw"}, {"type": "input_image", "image_url": PNG_URL}], "tools": [{"type": "image_generation", "n": 2}]}
        before = copy.deepcopy(body)
        self.normalize(body, "responses")
        self.assertEqual(body, before)

    def test_superseded_file_id_does_not_block_latest_inline_references(self) -> None:
        body = {"model": "gpt-image-2", "input": [
            {"role": "user", "content": [{"type": "input_image", "file_id": "old-file"}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": "edit the new image"},
                {"type": "input_image", "image_url": PNG_URL},
            ]},
        ]}
        result = self.normalize(body, "responses")
        self.assertEqual(result.request.images, [PNG_B64])

    def test_image_parameters_cannot_inject_arbitrary_upstream_prompt_text(self) -> None:
        for field, value, error in (
            ("size", "1024x1024 then add unrelated content", "size must"),
            ("size", "0x1024", "size must"),
            ("quality", "high then add unrelated content", "quality"),
        ):
            for options in ({field: value}, {"tools": [{"type": "image_generation", field: value}]}):
                self.assert_bad_request({"model": "gpt-image-2", "prompt": "draw", **options}, error)
        result = self.normalize({"model": "gpt-image-2", "prompt": "draw", "size": "3840X2160", "quality": "HIGH"})
        self.assertEqual((result.request.size, result.request.quality), ("3840x2160", "high"))
        self.assertTrue(result.request.strict_image_results)

    def test_image_25_extended_quality_values_remain_supported(self) -> None:
        for quality in ("xhigh", "max"):
            for protocol in ("chat", "responses"):
                result = self.normalize({"model": "gpt-image-2", "prompt": "draw", "quality": quality}, protocol)
                self.assertEqual(result.request.quality, quality)

    def test_protocol_must_be_one_of_frozen_values(self) -> None:
        with self.assertRaises(ValueError):
            self.normalize({"prompt": "draw"}, "images")
        with self.assertRaises(ValueError):
            self.compat.image_request_prompt({"prompt": "draw"}, protocol="images")

    def test_output_progress_and_result_metadata_are_preserved_without_mutation(self) -> None:
        progress = self.output("progress", text="working", upstream_event_type="synthetic.progress")
        result = self.output(data=[{"b64_json": " " + PNG_B64 + " ", "revised_prompt": "synthetic"}], created=123, conversation_id="synthetic-conversation")
        before = copy.deepcopy(result)
        outputs = list(self.compat.require_image_outputs([progress, result]))
        self.assertIs(outputs[0], progress)
        self.assertIsNot(outputs[1], result)
        self.assertEqual(outputs[1].created, 123)
        self.assertEqual(outputs[1].conversation_id, "synthetic-conversation")
        self.assertEqual(outputs[1].data, [{"b64_json": PNG_B64, "revised_prompt": "synthetic"}])
        self.assertEqual(result, before)

    def test_invalid_result_items_are_filtered_from_new_result(self) -> None:
        result = self.output(data=[{}, {"url": "https://synthetic.invalid/image.png"}, {"b64_json": "not base64!"}, {"b64_json": ""}, {"b64_json": "图片"}, {"b64_json": 123}, None, {"b64_json": PNG_B64}])
        before = copy.deepcopy(result)
        outputs = list(self.compat.require_image_outputs([result]))
        self.assertEqual(outputs[0].data, [{"b64_json": PNG_B64}])
        self.assertEqual(result, before)
        self.assertIsNot(outputs[0].data[0], result.data[-1])

    def test_empty_invalid_result_and_progress_only_streams_fail(self) -> None:
        for outputs in (
            [], [self.output("progress", text="working")], [self.output(data=[])],
            [self.output(data=[{"b64_json": "invalid!"}])],
            [self.output(data=[{"url": "https://synthetic.invalid/image.png"}])],
            [self.output(data=None)],
        ):
            self.assert_output_error(outputs, "no_image_generated")

    def test_message_always_fails_without_exposing_upstream_text(self) -> None:
        message = self.output("message", text="synthetic-private-upstream-text")
        for prefix in ([], [self.output(data=[{"b64_json": PNG_B64}])]):
            with self.assertRaises(self.conversation.ImageGenerationError) as caught:
                list(self.compat.require_image_outputs([*prefix, message]))
            self.assertEqual(caught.exception.code, "no_image_generated")
            self.assertNotIn(message.text, str(caught.exception))

    def test_total_count_spans_result_events_and_items(self) -> None:
        first = self.output(data=[{"b64_json": PNG_B64}, {"b64_json": PNG_B64}])
        second = self.output(data=[{"b64_json": PNG_B64}])
        self.assertEqual(len(list(self.compat.require_image_outputs([first, second], expected_count=3))), 2)
        self.assert_output_error([first, second], "incomplete_image_generation", expected_count=4)

    def test_invalid_items_cannot_satisfy_expected_count(self) -> None:
        result = self.output(data=[{"b64_json": PNG_B64}, {"b64_json": "invalid!"}])
        self.assert_output_error([result], "incomplete_image_generation", expected_count=2)

    def test_short_stream_reports_error_on_exhaustion_not_success(self) -> None:
        result = self.output(data=[{"b64_json": PNG_B64}])
        outputs = self.compat.require_image_outputs([result], expected_count=2)
        self.assertEqual(next(outputs).data, result.data)
        with self.assertRaises(self.conversation.ImageGenerationError) as caught:
            next(outputs)
        self.assertEqual(caught.exception.code, "incomplete_image_generation")
        self.assertIn("1 of 2", str(caught.exception))

    def test_empty_result_after_valid_result_still_fails(self) -> None:
        self.assert_output_error([self.output(data=[{"b64_json": PNG_B64}]), self.output(data=[])], "no_image_generated")

    def test_upstream_exception_is_not_swallowed(self) -> None:
        failure = self.conversation.ImageGenerationError("synthetic failure", code="synthetic_failure")

        def outputs():
            yield self.output("progress", text="working")
            raise failure

        with self.assertRaises(self.conversation.ImageGenerationError) as caught:
            list(self.compat.require_image_outputs(outputs()))
        self.assertIs(caught.exception, failure)

    def test_expected_count_must_be_positive_integer(self) -> None:
        for count in (0, -1, True, 1.0, "1"):
            with self.assertRaises(ValueError):
                list(self.compat.require_image_outputs([], expected_count=count))


if __name__ == "__main__":
    unittest.main()
