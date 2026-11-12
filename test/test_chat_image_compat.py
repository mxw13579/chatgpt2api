"""Synthetic Chat adapter fixtures; run only in the coordinator's isolated copy.

Backend entry points and text tokenization are mocked. No real service is called.
"""
from __future__ import annotations

import base64
import struct
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

from services.protocol import openai_v1_chat_complete as chat
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput


MODEL = "gpt-image-2"
PROMPT = "Draw a synthetic blue square."


def synthetic_png(rgb: tuple[int, int, int]) -> bytes:
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


IMAGE_BYTES = [synthetic_png((255, 0, 0)), synthetic_png((0, 0, 255))]
IMAGE_BASE64 = [base64.b64encode(data).decode("ascii") for data in IMAGE_BYTES]
IMAGE_URLS = ["data:image/png;base64," + data for data in IMAGE_BASE64]


def image_output(
    kind: str = "result",
    *,
    data: list[dict] | None = None,
    text: str = "",
    index: int = 1,
    total: int = 1,
) -> ImageOutput:
    if data is None:
        data = [{"b64_json": IMAGE_BASE64[0]}] if kind == "result" else []
    return ImageOutput(
        kind=kind, model=MODEL, index=index, total=total,
        created=123, text=text, data=data,
    )


class ChatImageCompatTests(unittest.TestCase):
    def patch_chat(self, name: str, **kwargs):
        patcher = mock.patch.object(chat, name, **kwargs)
        patched = patcher.start()
        self.addCleanup(patcher.stop)
        return patched

    def setUp(self) -> None:
        # Missed mocks fail closed rather than reaching an account or network backend.
        self.pool = self.patch_chat("stream_image_outputs_with_pool", return_value=iter(()))
        self.backend = self.patch_chat("text_backend", return_value=object())
        self.text = self.patch_chat("collect_text", return_value="Synthetic text answer.")
        self.deltas = self.patch_chat(
            "stream_text_deltas", side_effect=lambda *_args: iter(["Synthetic ", "text answer."]),
        )
        self.search = self.patch_chat("run_web_search", side_effect=AssertionError("Unexpected search"))
        self.text_tokens = self.patch_chat("count_text_tokens", side_effect=lambda text, _model: len(text))
        self.patch_chat("count_message_text_tokens", return_value=7)
        self.patch_chat("count_message_image_tokens", return_value=0)
        # Avoid ambient settings and shared cache state; test the adapter's own calls.
        self.patch_chat("normalize_messages", side_effect=lambda messages: messages)
        self.patch_chat("normalize_text_messages", side_effect=lambda messages: messages)
        self.cache = self.patch_chat("chat_completion_cache")
        self.cache.get_or_compute_response.side_effect = lambda _key, compute: compute()
        self.cache.get_or_compute_stream.side_effect = lambda _key, compute: compute()
        remote_get = mock.patch("utils.helper.requests.get", side_effect=AssertionError("Network is forbidden"))
        remote_get.start()
        self.addCleanup(remote_get.stop)

    def assert_no_text_path(self) -> None:
        self.backend.assert_not_called()
        self.text.assert_not_called()
        self.deltas.assert_not_called()
        self.search.assert_not_called()
        self.cache.get_or_compute_response.assert_not_called()
        self.cache.get_or_compute_stream.assert_not_called()

    def assert_image_failure(self, body: dict) -> None:
        chunks = []
        with self.assertRaises(ImageGenerationError):
            if body.get("stream"):
                for chunk in chat.handle(body):
                    chunks.append(chunk)
            else:
                chat.handle(body)
        self.assertFalse(any(chunk["choices"][0]["finish_reason"] == "stop" for chunk in chunks))
        self.assertFalse(any(
            chunk["choices"][0]["delta"].get("content") in {"", "Image generation completed."}
            for chunk in chunks
        ))

    def test_explicit_image_model_needs_no_tools_or_modalities(self) -> None:
        for model in (MODEL, "codex-gpt-image-2", "gpt-image-2.6-flare"):
            with self.subTest(model=model):
                body = {"model": model, "messages": [{"role": "user", "content": PROMPT}]}
                self.pool.reset_mock()
                outputs = iter([image_output()])
                self.pool.return_value = outputs
                with (
                    mock.patch.object(chat, "is_image_request", wraps=chat.is_image_request) as classify,
                    mock.patch.object(chat, "normalize_image_request", wraps=chat.normalize_image_request) as normalize,
                    mock.patch.object(chat, "require_image_outputs", wraps=chat.require_image_outputs) as require,
                ):
                    response = chat.handle(body)
                classify.assert_called_once_with(body)
                normalize.assert_called_once_with(body, protocol="chat")
                require.assert_called_once_with(outputs, expected_count=1)
                self.pool.assert_called_once()
                request = self.pool.call_args.args[0]
                self.assertEqual((request.model, request.prompt, request.n), (model, PROMPT, 1))
                self.assertEqual(request.response_format, "b64_json")
                self.assertTrue(request.message_as_error)
                self.assertEqual(response["object"], "chat.completion")
                self.assertEqual(response["model"], model)
                self.assertEqual(response["created"], 123)
                choice = response["choices"][0]
                self.assertEqual(choice["message"], {
                    "role": "assistant", "content": f"![image_1]({IMAGE_URLS[0]})",
                })
                self.assertEqual(choice["finish_reason"], "stop")
        self.assert_no_text_path()

    def test_prompt_and_responses_input_are_accepted_on_chat_image_path(self) -> None:
        payloads = [
            {"prompt": PROMPT},
            {"input": PROMPT},
            {"input": [{"type": "input_text", "text": PROMPT}]},
            {"input": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": PROMPT}],
            }]},
        ]
        for stream in (False, True):
            for payload in payloads:
                with self.subTest(stream=stream, payload=payload):
                    self.pool.return_value = iter([image_output()])
                    result = chat.handle({"model": MODEL, "stream": stream, **payload})
                    if stream:
                        chunks = list(result)
                        content = chat.collect_chat_content(chunks)
                        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
                    else:
                        content = result["choices"][0]["message"]["content"]
                    self.assertEqual(self.pool.call_args.args[0].prompt, PROMPT)
                    self.assertIn(IMAGE_URLS[0], content)
        self.assert_no_text_path()

    def test_parameters_and_two_reference_images_reach_pool_in_both_modes(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = {
                    "model": MODEL, "stream": stream, "n": 2,
                    "size": "1536x1024", "quality": "high", "response_format": "url",
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": PROMPT},
                        *[{"type": "image_url", "image_url": {"url": url}} for url in IMAGE_URLS],
                    ]}],
                }
                # One result can contain two images: expected_count counts images, not events.
                outputs = iter([image_output(data=[{"b64_json": data} for data in IMAGE_BASE64], total=2)])
                self.pool.return_value = outputs
                with mock.patch.object(chat, "require_image_outputs", wraps=chat.require_image_outputs) as require:
                    result = chat.handle(body)
                    content = chat.collect_chat_content(result) if stream else result["choices"][0]["message"]["content"]
                require.assert_called_once_with(outputs, expected_count=2)
                request = self.pool.call_args.args[0]
                self.assertEqual(request.model, MODEL)
                self.assertEqual(request.prompt, PROMPT)
                self.assertEqual(request.n, 2)
                self.assertEqual(request.size, "1536x1024")
                self.assertEqual(request.quality, "high")
                self.assertEqual(request.images, IMAGE_BASE64)
                self.assertEqual(request.response_format, "b64_json")
                self.assertTrue(request.message_as_error)
                for url in IMAGE_URLS:
                    self.assertIn(url, content)
                if not stream:
                    self.assertGreater(result["usage"]["prompt_tokens_details"]["image_tokens"], 0)
                    self.assertGreater(result["usage"]["completion_tokens_details"]["image_tokens"], 0)
        self.assert_no_text_path()

    def test_normalized_request_and_raw_images_are_used_for_generation_and_usage(self) -> None:
        raw_images = [(data, f"synthetic_{index}.png", "image/png") for index, data in enumerate(IMAGE_BYTES)]
        request = ConversationRequest(
            model=MODEL, prompt="Normalized synthetic prompt.", n=2,
            size="1536x1024", quality="high", images=list(IMAGE_BASE64),
            response_format="b64_json", message_as_error=True,
        )
        normalized = SimpleNamespace(request=request, images=raw_images)
        body = {"model": MODEL, "prompt": "Unnormalized prompt.", "n": 1, "size": "auto", "quality": "low"}
        data = [{"b64_json": value} for value in IMAGE_BASE64]
        self.pool.return_value = iter([image_output(data=data, total=2)])
        self.text_tokens.side_effect = None
        self.text_tokens.return_value = 11
        with (
            mock.patch.object(chat, "normalize_image_request", return_value=normalized) as normalize,
            mock.patch.object(chat, "count_image_inputs_tokens", return_value=19) as inputs,
            mock.patch.object(chat, "count_image_output_items_tokens", return_value=23) as outputs,
            mock.patch.object(chat, "require_image_outputs", wraps=chat.require_image_outputs) as require,
        ):
            response = chat.handle(body)
        normalize.assert_called_once_with(body, protocol="chat")
        self.pool.assert_called_once_with(request)
        self.assertIs(self.pool.call_args.args[0], request)
        self.assertEqual(require.call_args.kwargs, {"expected_count": 2})
        self.text_tokens.assert_called_once_with(request.prompt, request.model)
        inputs.assert_called_once_with(raw_images, request.model)
        self.assertIs(inputs.call_args.args[0], raw_images)
        outputs.assert_called_once_with(data, request.size, request.quality)
        self.assertEqual(response["usage"], {
            "prompt_tokens": 30, "completion_tokens": 23, "total_tokens": 53,
            "prompt_tokens_details": {"text_tokens": 11, "image_tokens": 19, "cached_tokens": 0},
            "completion_tokens_details": {"text_tokens": 0, "image_tokens": 23, "reasoning_tokens": 0},
        })

    def test_stream_passes_normalized_request_unchanged_and_checks_normalized_count(self) -> None:
        request = ConversationRequest(
            model=MODEL, prompt=PROMPT, n=2, size="1536x1024", quality="high",
            images=list(IMAGE_BASE64), response_format="b64_json", message_as_error=True,
        )
        normalized = SimpleNamespace(request=request, images=[])
        body = {"model": MODEL, "input": PROMPT, "n": 1, "stream": True}
        # The body asks for one, but the normalized contract asks for two.
        self.pool.return_value = iter([image_output()])
        with (
            mock.patch.object(chat, "normalize_image_request", return_value=normalized) as normalize,
            mock.patch.object(chat, "require_image_outputs", wraps=chat.require_image_outputs) as require,
        ):
            self.assert_image_failure(body)
        normalize.assert_called_once_with(body, protocol="chat")
        self.pool.assert_called_once_with(request)
        self.assertIs(self.pool.call_args.args[0], request)
        self.assertEqual(require.call_args.kwargs, {"expected_count": 2})
        self.assert_no_text_path()

    def test_legacy_image_args_is_a_thin_normalizer_wrapper(self) -> None:
        raw_images = [(IMAGE_BYTES[0], "synthetic.png", "image/png")]
        request = ConversationRequest(model=MODEL, prompt=PROMPT, n=2)
        body = {"model": MODEL, "input": PROMPT}
        with mock.patch.object(chat, "normalize_image_request", return_value=SimpleNamespace(
            request=request, images=raw_images,
        )) as normalize:
            args = chat.chat_image_args(body)
        normalize.assert_called_once_with(body, protocol="chat")
        self.assertEqual(args, (MODEL, PROMPT, 2, raw_images))
        self.assertIs(args[3], raw_images)

    def test_no_image_message_and_partial_outputs_never_succeed(self) -> None:
        cases = [
            ("empty", 1, []),
            ("progress_only", 1, [image_output("progress", text="Working on synthetic image.")]),
            ("message_only", 1, [image_output("message", text="No synthetic image generated.")]),
            ("empty_result", 1, [image_output(data=[])]),
            ("empty_base64", 1, [image_output(data=[{"b64_json": ""}])]),
            ("url_only", 1, [image_output(data=[{"url": "https://example.test/synthetic.png"}])]),
            ("partial_count", 2, [image_output(total=2)]),
            ("partial_message", 2, [image_output(total=2), image_output("message", text="Synthetic failure.")]),
            ("message_after_required_image", 1, [image_output(), image_output("message", text="Synthetic failure.")]),
            ("mixed_inline_and_url", 2, [image_output(data=[
                {"b64_json": IMAGE_BASE64[0]}, {"url": "https://example.test/synthetic.png"},
            ], total=2)]),
        ]
        for stream in (False, True):
            for name, n, outputs in cases:
                with self.subTest(stream=stream, case=name):
                    self.pool.return_value = iter(outputs)
                    self.assert_image_failure({"model": MODEL, "prompt": PROMPT, "stream": stream, "n": n})
        self.assert_no_text_path()

    def test_upstream_failure_after_image_is_not_converted_to_stop(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                failure = ImageGenerationError("Synthetic upstream failure.")

                def fail_after_image():
                    yield image_output()
                    raise failure

                self.pool.return_value = fail_after_image()
                self.assert_image_failure({"model": MODEL, "prompt": PROMPT, "stream": stream})

    def test_stream_has_inline_markdown_consistent_metadata_and_one_final_stop(self) -> None:
        self.pool.return_value = iter([
            image_output("progress", text="Drawing synthetic images.\n", total=2),
            image_output(data=[{"b64_json": IMAGE_BASE64[0]}], index=1, total=2),
            image_output(data=[{"b64_json": IMAGE_BASE64[1]}], index=2, total=2),
        ])
        chunks = list(chat.handle({"model": MODEL, "prompt": PROMPT, "n": 2, "stream": True}))
        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(len({chunk["id"] for chunk in chunks}), 1)
        self.assertEqual(len({chunk["created"] for chunk in chunks}), 1)
        self.assertTrue(chunks[0]["id"].startswith("chatcmpl-"))
        for chunk in chunks:
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertEqual(chunk["model"], MODEL)
            self.assertEqual(chunk["choices"][0]["index"], 0)
        for chunk in chunks[:-1]:
            self.assertIsNone(chunk["choices"][0]["finish_reason"])
            self.assertTrue(chunk["choices"][0]["delta"]["content"])
        self.assertEqual(sum("role" in chunk["choices"][0]["delta"] for chunk in chunks), 1)
        self.assertEqual(chunks[-1]["choices"][0], {"index": 0, "delta": {}, "finish_reason": "stop"})
        content = chat.collect_chat_content(chunks)
        self.assertTrue(content.startswith("Drawing synthetic images.\n"))
        for url in IMAGE_URLS:
            self.assertIn(f"]({url})", content)
        self.assertEqual(content.count("![image_"), 2)
        self.assertNotIn("Image generation completed.", content)
        self.assert_no_text_path()

    def test_direct_image_stream_also_rejects_missing_images(self) -> None:
        for outputs in ([], [image_output("progress", text="Working.")], [image_output("message", text="No image.")]):
            with self.subTest(kinds=[output.kind for output in outputs]):
                chunks = []
                with self.assertRaises(ImageGenerationError):
                    for chunk in chat.stream_image_chat_completion(outputs, MODEL):
                        chunks.append(chunk)
                self.assertFalse(any(chunk["choices"][0]["finish_reason"] == "stop" for chunk in chunks))

    def test_gpt_5_5_stays_text_even_with_reference_images(self) -> None:
        for stream in (False, True):
            for references in (False, True):
                with self.subTest(stream=stream, references=references):
                    content = [
                        {"type": "text", "text": "Describe this synthetic image."},
                        {"type": "image_url", "image_url": {"url": IMAGE_URLS[0]}},
                    ] if references else "Say hello."
                    messages = [{"role": "user", "content": content}]
                    body = {"model": "gpt-5-5", "messages": messages, "stream": stream, "reasoning_effort": "xhigh"}
                    result = chat.handle(body)
                    if stream:
                        chunks = list(result)
                        self.assertEqual(chat.collect_chat_content(chunks), "Synthetic text answer.")
                        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
                        request = self.deltas.call_args.args[1]
                    else:
                        self.assertEqual(result["object"], "chat.completion")
                        self.assertEqual(result["choices"][0]["message"]["content"], "Synthetic text answer.")
                        request = self.text.call_args.args[1]
                    self.assertEqual(request.model, "gpt-5-5")
                    self.assertEqual(request.messages, messages)
                    self.assertEqual(request.thinking_effort, "extended")
        self.pool.assert_not_called()
        self.search.assert_not_called()

    def test_text_cache_keys_have_chat_namespace_in_both_modes(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                messages = [{"role": "user", "content": "Synthetic cache prompt."}]
                body = {"model": "gpt-5-5", "messages": messages, "stream": stream}
                with mock.patch.object(chat, "cache_key", return_value="shared-digest") as key:
                    result = chat.handle(body)
                    if stream:
                        list(result)
                key.assert_called_once_with(body, messages, stream=stream)
                cache_call = self.cache.get_or_compute_stream if stream else self.cache.get_or_compute_response
                self.assertEqual(cache_call.call_args.args[0], "chat:shared-digest")
                self.assertNotEqual(cache_call.call_args.args[0], "responses:shared-digest")
        self.pool.assert_not_called()

    def test_web_search_keeps_chat_response_and_stream_shapes(self) -> None:
        self.search.side_effect = None
        self.search.return_value = {
            "answer": "Synthetic search answer.",
            "sources": [{"title": "Synthetic source", "url": "https://example.test/source", "snippet": "Fixture."}],
        }
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.search.reset_mock()
                body = {
                    "model": "gpt-5-5", "stream": stream,
                    "messages": [{"role": "user", "content": "Synthetic search query."}],
                    "tools": [{"type": "web_search"}],
                }
                result = chat.handle(body)
                if stream:
                    chunks = list(result)
                    self.assertTrue(all(chunk["object"] == "chat.completion.chunk" for chunk in chunks))
                    self.assertIn("Synthetic search answer.", chat.collect_chat_content(chunks))
                    self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
                else:
                    message = result["choices"][0]["message"]
                    self.assertEqual(result["object"], "chat.completion")
                    self.assertIn("Synthetic search answer.", message["content"])
                    self.assertEqual(message["annotations"][0]["url_citation"]["url"], "https://example.test/source")
                self.search.assert_called_once_with("Synthetic search query.")
        self.pool.assert_not_called()
        self.backend.assert_not_called()
        self.cache.get_or_compute_response.assert_not_called()
        self.cache.get_or_compute_stream.assert_not_called()

    def test_unsupported_text_tools_keep_honesty_guard(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = {
                    "model": "gpt-5-5", "stream": stream,
                    "messages": [{"role": "user", "content": "Discuss this synthetic command without executing it."}],
                    "tools": [{"type": "function", "function": {"name": "synthetic_shell"}}],
                }
                result = chat.handle(body)
                if stream:
                    list(result)
                request = (self.deltas if stream else self.text).call_args.args[1]
                self.assertEqual(request.messages[0], {"role": "system", "content": chat.TOOL_UNAVAILABLE_SYSTEM_MESSAGE})
        self.pool.assert_not_called()
        self.search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
