"""Mock-only Responses tests; run with the main thread's isolated configuration."""
from __future__ import annotations

import base64
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from services.protocol import openai_v1_response as responses
from services.protocol.conversation import ConversationRequest, ImageGenerationError, ImageOutput
from utils.helper import sse_json_stream


MODEL = "gpt-image-2"
PROMPT = "Draw a synthetic blue circle."
PNG_FIRST = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lm2C6wAAAABJRU5ErkJggg=="
PNG_SECOND = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP8z8BQDwAFgwJ/luzl4wAAAABJRU5ErkJggg=="
DATA_FIRST = "data:image/png;base64," + PNG_FIRST
DATA_SECOND = "data:image/png;base64," + PNG_SECOND


def image_result(*images: str, index: int = 1, total: int = 1) -> ImageOutput:
    return ImageOutput(
        kind="result",
        model=MODEL,
        index=index,
        total=total,
        data=[{"b64_json": image, "revised_prompt": PROMPT} for image in images],
    )


def text_only_output() -> ImageOutput:
    return ImageOutput(kind="message", model=MODEL, index=1, total=1, text="Synthetic text, not an image.")


class ResponsesImageCompatTests(unittest.TestCase):
    def patch_response(self, name: str, **kwargs):
        patcher = mock.patch.object(responses, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self) -> None:
        catalog = mock.patch("utils.helper.configured_web_image_models", return_value={MODEL})
        catalog.start()
        self.addCleanup(catalog.stop)
        self.outputs = [image_result(PNG_FIRST)]
        self.pool = self.patch_response("stream_image_outputs_with_pool", side_effect=lambda _request: iter(self.outputs))
        self.normalizer = self.patch_response("normalize_image_request", wraps=responses.normalize_image_request)
        self.validator = self.patch_response("require_image_outputs", wraps=responses.require_image_outputs)
        self.input_tokens = self.patch_response("count_image_inputs_tokens", return_value=13)
        self.output_tokens = self.patch_response(
            "count_image_output_items_tokens", side_effect=lambda items, _size, _quality: 11 * len(items),
        )
        # Tokenizers may lazily fetch encoding assets: no tokenizer runs in these tests.
        self.patch_response("count_text_tokens", return_value=7)
        self.patch_response("count_message_text_tokens", return_value=7)
        self.patch_response("count_message_image_tokens", return_value=0)
        self.patch_response("normalize_messages", side_effect=lambda messages: messages)
        self.patch_response("normalize_text_messages", side_effect=lambda messages: messages)
        self.text_backend = self.patch_response("text_backend", side_effect=AssertionError("Unexpected text backend"))
        self.text_deltas = self.patch_response("stream_text_deltas", side_effect=AssertionError("Unexpected text stream"))
        self.cache = self.patch_response("chat_completion_cache")
        self.cache.get_or_compute_stream.side_effect = AssertionError("Unexpected text cache")

    def test_image_model_without_tools_routes_to_images(self) -> None:
        body = {"model": MODEL, "input": PROMPT}

        self.assertFalse(responses.is_text_response_request(body))
        response = responses.handle(body)

        self.normalizer.assert_called_once_with(body, protocol="responses")
        request = self.pool.call_args.args[0]
        self.assertEqual(request.model, MODEL)
        self.assertEqual(request.prompt, PROMPT)
        self.assertTrue(request.message_as_error)
        self.assertEqual(request.response_format, "b64_json")
        self.assertEqual(response["status"], "completed")
        self.assertEqual([item["result"] for item in response["output"]], [PNG_FIRST])
        self.text_backend.assert_not_called()
        self.cache.get_or_compute_stream.assert_not_called()

    def test_routing_delegates_to_shared_image_classifier(self) -> None:
        body = {"model": MODEL, "input": PROMPT}
        for is_image in (False, True):
            with self.subTest(is_image=is_image), mock.patch.object(
                responses, "is_image_request", return_value=is_image,
            ) as classifier:
                self.assertEqual(responses.is_text_response_request(body), not is_image)
                classifier.assert_called_once_with(body)

    def test_image_tool_still_selects_image_route_for_ordinary_model(self) -> None:
        body = {"model": "auto", "input": PROMPT, "tools": [{"type": "image_generation", "model": MODEL}]}

        self.assertFalse(responses.is_text_response_request(body))
        response = responses.handle(body)

        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.pool.assert_called_once()
        self.text_backend.assert_not_called()

    def test_input_string_input_messages_and_misplaced_messages_or_prompt(self) -> None:
        variants = [
            {"input": PROMPT},
            {"input": [{"role": "user", "content": PROMPT}]},
            {"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": PROMPT}]}]},
            {"messages": [{"role": "user", "content": PROMPT}]},
            {"messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]},
            {"prompt": PROMPT},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                body = {"model": MODEL, **variant}
                response = responses.handle(body)
                request = self.pool.call_args.args[0]

                self.normalizer.assert_called_with(body, protocol="responses")
                self.assertEqual(request.prompt, PROMPT)
                self.assertEqual(response["output"][0]["result"], PNG_FIRST)

    def test_multiple_reference_images_survive_input_and_messages_normalization(self) -> None:
        variants = [
            {"input": [{"role": "user", "content": [
                {"type": "input_text", "text": PROMPT},
                {"type": "input_image", "image_url": DATA_FIRST},
                {"type": "input_image", "image_url": DATA_SECOND},
            ]}]},
            {"messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": DATA_FIRST}},
                {"type": "image_url", "image_url": {"url": DATA_SECOND}},
            ]}]},
        ]
        with mock.patch.object(responses, "extract_response_image", side_effect=AssertionError("Legacy first-image path")):
            for variant in variants:
                with self.subTest(field=next(iter(variant))):
                    responses.handle({"model": MODEL, **variant})
                    request = self.pool.call_args.args[0]

                    self.assertEqual(request.images, [PNG_FIRST, PNG_SECOND])
                    self.assertTrue(request.message_as_error)
                    raw_images, model = self.input_tokens.call_args.args
                    self.assertEqual(model, request.model)
                    self.assertEqual([image[0] for image in raw_images], [
                        base64.b64decode(PNG_FIRST), base64.b64decode(PNG_SECOND),
                    ])
                    self.assertEqual([image[2] for image in raw_images], ["image/png", "image/png"])

    def test_normalized_conversation_request_is_passed_to_pool_unchanged(self) -> None:
        request = ConversationRequest(
            model=MODEL, prompt=PROMPT, n=2, size="1536x1024", quality="high",
            images=[PNG_FIRST, PNG_SECOND], response_format="b64_json", message_as_error=True,
        )
        raw_images = [
            (base64.b64decode(PNG_FIRST), "synthetic-first.png", "image/png"),
            (base64.b64decode(PNG_SECOND), "synthetic-second.png", "image/png"),
        ]
        self.normalizer.return_value = SimpleNamespace(request=request, images=raw_images)
        self.outputs = [image_result(PNG_FIRST), image_result(PNG_SECOND)]

        response = responses.handle({"model": MODEL, "prompt": PROMPT})

        self.pool.assert_called_once_with(request)
        self.assertIs(self.pool.call_args.args[0], request)
        self.input_tokens.assert_called_once_with(raw_images, MODEL)
        self.assertEqual(self.validator.call_args.kwargs, {"expected_count": 2})
        self.assertEqual(len(response["output"]), 2)
        for call in self.output_tokens.call_args_list:
            self.assertEqual(call.args[1:], ("1536x1024", "high"))

    def test_size_quality_and_n_are_forwarded_from_body_or_image_tool(self) -> None:
        self.outputs = [image_result(PNG_FIRST, index=1, total=2), image_result(PNG_SECOND, index=2, total=2)]
        variants = [
            {"size": "1536x1024", "quality": "high"},
            {"tools": [{"type": "image_generation", "size": "1536x1024", "quality": "high"}]},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                response = responses.handle({"model": MODEL, "input": PROMPT, "n": 2, "response_format": "url", **variant})
                request = self.pool.call_args.args[0]

                self.assertEqual((request.n, request.size, request.quality), (2, "1536x1024", "high"))
                self.assertEqual(request.response_format, "b64_json")
                self.assertTrue(request.message_as_error)
                self.assertEqual([item["result"] for item in response["output"]], [PNG_FIRST, PNG_SECOND])
                self.assertEqual(self.validator.call_args.kwargs, {"expected_count": 2})

    def test_multiple_result_events_accumulate_in_non_stream_response_and_usage(self) -> None:
        self.outputs = [image_result(PNG_FIRST, index=1, total=2), image_result(PNG_SECOND, index=2, total=2)]

        response = responses.handle({"model": MODEL, "input": PROMPT, "n": 2})

        self.assertEqual(response["status"], "completed")
        self.assertEqual([item["result"] for item in response["output"]], [PNG_FIRST, PNG_SECOND])
        self.assertEqual(len({item["id"] for item in response["output"]}), 2)
        self.assertEqual(response["usage"]["input_tokens"], 20)
        self.assertEqual(response["usage"]["output_tokens"], 22)
        self.assertEqual(response["usage"]["total_tokens"], 42)

    def test_direct_stream_call_preserves_all_images_in_one_result(self) -> None:
        events = list(responses.stream_image_response([image_result(PNG_FIRST, PNG_SECOND)], PROMPT, MODEL))

        done = [event for event in events if event["type"] == "response.output_item.done"]
        self.assertEqual([event["output_index"] for event in done], [0, 1])
        self.assertEqual([item["result"] for item in events[-1]["response"]["output"]], [PNG_FIRST, PNG_SECOND])
        self.assertEqual(len({event["item"]["id"] for event in done}), 2)

    def test_direct_stream_consumes_later_results_even_when_default_count_is_met(self) -> None:
        events = list(responses.stream_image_response(
            [image_result(PNG_FIRST, PNG_SECOND), image_result(PNG_FIRST)], PROMPT, MODEL,
        ))

        done = [event for event in events if event["type"] == "response.output_item.done"]
        self.assertEqual([event["output_index"] for event in done], [0, 1, 2])
        self.assertEqual(len(events[-1]["response"]["output"]), 3)
        self.assertEqual(len({event["item"]["id"] for event in done}), 3)

    def test_explicit_item_id_remains_unique_for_a_batch(self) -> None:
        items = responses.image_output_items(PROMPT, [{"b64_json": PNG_FIRST}, {"b64_json": PNG_SECOND}], "ig_fixture")

        self.assertEqual(items[0]["id"], "ig_fixture")
        self.assertEqual(len({item["id"] for item in items}), 2)
        self.assertEqual([item["type"] for item in items], ["image_generation_call", "image_generation_call"])
        self.assertEqual([item["result"] for item in items], [PNG_FIRST, PNG_SECOND])

    def test_stream_emits_added_done_and_one_completed_without_fake_previews(self) -> None:
        self.outputs = [
            ImageOutput(kind="progress", model=MODEL, index=1, total=2, text="Synthetic progress"),
            image_result(PNG_FIRST, index=1, total=2),
            ImageOutput(kind="progress", model=MODEL, index=2, total=2, text="Synthetic progress"),
            image_result(PNG_SECOND, index=2, total=2),
        ]
        events = list(responses.handle({
            "model": MODEL, "input": PROMPT, "n": 2, "stream": True,
            "tools": [{"type": "image_generation", "partial_images": 0}],
        }))

        self.assertEqual([event["type"] for event in events], [
            "response.created", "response.output_item.added", "response.output_item.done",
            "response.output_item.added", "response.output_item.done", "response.completed",
        ])
        added = [events[1], events[3]]
        done = [events[2], events[4]]
        self.assertEqual([event["output_index"] for event in added], [0, 1])
        self.assertEqual([event["output_index"] for event in done], [0, 1])
        self.assertEqual([event["item"]["status"] for event in added], ["in_progress", "in_progress"])
        self.assertEqual([event["item"].get("result") for event in added], [None, None])
        self.assertEqual([event["item"]["status"] for event in done], ["completed", "completed"])
        self.assertEqual([event["item"]["id"] for event in added], [event["item"]["id"] for event in done])
        self.assertEqual(len({event["item"]["id"] for event in done}), 2)
        self.assertEqual(events[0]["response"]["id"], events[-1]["response"]["id"])
        self.assertEqual(events[0]["response"]["status"], "in_progress")
        self.assertEqual(events[0]["response"]["output"], [])
        self.assertEqual(events[-1]["response"]["output"], [event["item"] for event in done])

    def test_partial_image_preview_requests_are_rejected_not_fabricated(self) -> None:
        body = {
            "model": MODEL, "input": PROMPT,
            "tools": [{"type": "image_generation", "partial_images": 3}],
        }
        with self.assertRaises(HTTPException) as caught:
            responses.handle(body)
        self.assertEqual(caught.exception.status_code, 400)

        events = list(responses.handle({**body, "stream": True}))

        self.assertEqual([event["type"] for event in events], ["response.failed"])
        self.assertEqual(events[0]["response"]["status"], "failed")
        self.assertEqual(events[0]["response"]["error"]["type"], "invalid_request_error")
        self.pool.assert_not_called()

    def test_empty_message_only_and_short_results_raise_for_non_stream(self) -> None:
        cases = [
            ("empty", [], 1),
            ("empty_result", [image_result()], 1),
            ("blank_image", [image_result(" ")], 1),
            ("message_only", [text_only_output()], 1),
            ("short", [image_result(PNG_FIRST)], 2),
        ]
        for label, outputs, count in cases:
            with self.subTest(case=label):
                self.outputs = outputs
                with self.assertRaises(ImageGenerationError) as caught:
                    responses.handle({"model": MODEL, "input": PROMPT, "n": count})
                if count == 1:
                    self.assertEqual(caught.exception.code, "no_image_generated")

    def test_collect_response_propagates_image_failure_instead_of_returning_failed_object(self) -> None:
        with self.assertRaises(ImageGenerationError) as caught:
            responses.collect_response(responses.stream_image_response([text_only_output()], PROMPT, MODEL))

        self.assertEqual(caught.exception.code, "no_image_generated")

    def test_empty_message_only_and_short_streams_fail_without_completed(self) -> None:
        for label, outputs, count in [
            ("empty", [], 1),
            ("message_only", [text_only_output()], 1),
            ("short", [image_result(PNG_FIRST)], 2),
        ]:
            with self.subTest(case=label):
                self.outputs = outputs
                events = list(responses.handle({"model": MODEL, "input": PROMPT, "n": count, "stream": True}))
                types = [event["type"] for event in events]
                failure = events[-1]["response"]

                self.assertEqual(types[0], "response.created")
                self.assertEqual(types[-1], "response.failed")
                self.assertNotIn("response.completed", types)
                self.assertNotIn("response.output_text.delta", types)
                self.assertEqual(failure["status"], "failed")
                self.assertTrue(failure["error"])
                self.assertEqual(failure["id"], events[0]["response"]["id"])
                self.assertEqual(events[0]["response"]["status"], "in_progress")
                if count == 1:
                    self.assertEqual(failure["error"]["code"], "no_image_generated")
                    self.assertEqual(failure["output"], [])
                else:
                    self.assertEqual([item["result"] for item in failure["output"]], [PNG_FIRST])

    def test_failure_after_enough_images_is_not_hidden_by_early_completion(self) -> None:
        error = RuntimeError("Synthetic internal upstream diagnostic")

        def failing_outputs(_request):
            yield image_result(PNG_FIRST)
            raise error

        self.pool.side_effect = failing_outputs
        events = list(responses.handle({"model": MODEL, "input": PROMPT, "stream": True}))

        self.assertEqual([event["type"] for event in events], [
            "response.created", "response.output_item.added", "response.output_item.done", "response.failed",
        ])
        self.assertNotIn(str(error), json.dumps(events))
        with self.assertRaises(RuntimeError) as caught:
            responses.handle({"model": MODEL, "input": PROMPT})
        self.assertIs(caught.exception, error)

    def test_normalization_error_is_typed_only_for_stream_and_stays_http_error_otherwise(self) -> None:
        error = HTTPException(status_code=400, detail="Synthetic private validation detail")
        self.normalizer.side_effect = error

        events = list(responses.handle({"model": MODEL, "input": PROMPT, "stream": True}))

        self.assertEqual([event["type"] for event in events], ["response.failed"])
        self.assertEqual(events[0]["response"]["status"], "failed")
        self.assertEqual(events[0]["response"]["error"]["type"], "invalid_request_error")
        self.assertEqual(events[0]["response"]["error"]["code"], "bad_request")
        self.assertNotIn(error.detail, json.dumps(events))
        with self.assertRaises(HTTPException) as caught:
            responses.handle({"model": MODEL, "input": PROMPT})
        self.assertIs(caught.exception, error)
        self.pool.assert_not_called()

    def test_stream_error_does_not_expose_account_or_upstream_details(self) -> None:
        error = ImageGenerationError(
            "Synthetic private upstream detail", code="no_image_generated",
            account_email="fixture@example.invalid", conversation_id="synthetic-private-conversation",
        )
        self.pool.side_effect = error

        events = list(responses.handle({"model": MODEL, "input": PROMPT, "stream": True}))
        failure = events[-1]["response"]

        self.assertEqual(events[-1]["type"], "response.failed")
        self.assertEqual(failure["error"]["code"], "no_image_generated")
        serialized = json.dumps(events)
        for private_value in (str(error), error.account_email, error.conversation_id):
            self.assertNotIn(private_value, serialized)
        self.assertNotIn("response.completed", [event["type"] for event in events])

    def test_generic_sse_helper_receives_typed_failure_not_an_exception(self) -> None:
        self.outputs = []
        with mock.patch("utils.helper.logger") as logger:
            frames = list(sse_json_stream(responses.handle({"model": MODEL, "input": PROMPT, "stream": True})))

        payloads = [
            json.loads(frame[6:].strip()) for frame in frames
            if frame.startswith("data: ") and frame.strip() != "data: [DONE]"
        ]
        self.assertEqual([payload["type"] for payload in payloads], ["response.created", "response.failed"])
        self.assertEqual(payloads[-1]["response"]["error"]["code"], "no_image_generated")
        logger.warning.assert_not_called()

    def test_ordinary_model_without_tools_stays_text_and_uses_responses_cache_namespace(self) -> None:
        self.text_backend.side_effect = None
        self.text_backend.return_value = object()
        self.text_deltas.side_effect = lambda _backend, _request: iter(["Synthetic text reply"])
        self.cache.get_or_compute_stream.side_effect = lambda _key, compute: compute()
        messages = [{"role": "user", "content": PROMPT}]

        with mock.patch.object(responses, "cache_key", return_value="synthetic-cache-key") as key:
            for stream in (False, True):
                with self.subTest(stream=stream):
                    body = {"model": "auto", "input": PROMPT, "stream": stream}
                    self.assertTrue(responses.is_text_response_request(body))
                    result = responses.handle(body)
                    response = list(result)[-1]["response"] if stream else result

                    self.assertEqual(response["status"], "completed")
                    self.assertEqual(response["output"][0]["type"], "message")
                    self.assertEqual(response["output"][0]["content"][0]["text"], "Synthetic text reply")
                    key.assert_called_with(body, messages, stream=stream)
                    self.assertEqual(self.cache.get_or_compute_stream.call_args.args[0], "responses:synthetic-cache-key")
        self.normalizer.assert_not_called()
        self.pool.assert_not_called()
        self.validator.assert_not_called()

    def test_text_stream_errors_are_not_rewrapped_as_image_failures(self) -> None:
        error = RuntimeError("Synthetic text failure")
        self.text_backend.side_effect = error
        self.cache.get_or_compute_stream.side_effect = lambda _key, compute: compute()

        with self.assertRaises(RuntimeError) as caught:
            list(responses.handle({"model": "auto", "input": PROMPT, "stream": True}))

        self.assertIs(caught.exception, error)
        self.normalizer.assert_not_called()
        self.pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
