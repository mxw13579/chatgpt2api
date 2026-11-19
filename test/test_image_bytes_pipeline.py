"""Synthetic bytes-pipeline tests; run only through test/run_isolated.py."""

from __future__ import annotations

import base64
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from services import openai_backend_api as backend_module
from services.image_storage_service import StoredImage
from services.protocol import conversation
from utils import image_tokens

IMAGE_BYTES = [b"synthetic-first-image", b"synthetic-second-image"]
IMAGE_B64 = [base64.b64encode(data).decode("ascii") for data in IMAGE_BYTES]
BASE_URL = "https://synthetic.invalid"
IMAGE_URLS = [BASE_URL + "/first.png", BASE_URL + "/second.png"]
STORED_IMAGES = [
    StoredImage(rel=url.rsplit("/", 1)[-1], url=url, storage="local", size=len(data))
    for url, data in zip(IMAGE_URLS, IMAGE_BYTES)
]


class ImageBytesOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = mock.Mock(spec=[
            "resolve_conversation_image_urls", "download_image_bytes", "iter_codex_image_response_events",
        ])
        self.backend.resolve_conversation_image_urls.return_value = IMAGE_URLS
        self.backend.download_image_bytes.return_value = IMAGE_BYTES
        self.event = {
            "type": "conversation.completed", "conversation_id": "synthetic-conversation",
            "file_ids": ["file-synthetic"], "text": "", "turn_use_case": "image gen",
        }
        self.save = mock.Mock(side_effect=STORED_IMAGES)
        for name, value in (
            ("conversation_events", mock.Mock(return_value=[self.event])),
            ("image_storage_service", SimpleNamespace(save=self.save)),
            ("_image_remaining_timeout_secs", mock.Mock(return_value=30.0)),
            ("config", SimpleNamespace(image_poll_initial_wait_secs=0)),
            ("logger", mock.Mock()),
        ):
            patcher = mock.patch.object(conversation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_turn_that_did_not_draw_is_retried_once_with_tool_instruction(self) -> None:
        analysis = [
            {"type": "conversation.delta", "conversation_id": "first", "delta": "背景是蓝色"},
            {"type": "conversation.completed", "conversation_id": "first", "text": "背景是蓝色", "turn_use_case": "data analysis"},
        ]
        request = conversation.ConversationRequest(model="gpt-image-2", prompt="图片背景是什么颜色", images=["data:image/png;base64,AA=="])

        with (
            mock.patch.object(conversation, "_remove_image_conversation_later") as remove,
            mock.patch.object(conversation, "_get_detailed_error_from_tasks", return_value=""),
        ):
            conversation.conversation_events.side_effect = [analysis, [self.event]]
            outputs = list(conversation.stream_image_outputs(self.backend, request))
            prompts = [call.kwargs["prompt"] for call in conversation.conversation_events.call_args_list]
            self.assertEqual(prompts, [request.prompt, conversation.IMAGE_TOOL_INSTRUCTION + request.prompt])
            self.assertEqual([output.kind for output in outputs], ["result"])
            remove.assert_called_once_with(self.backend, "first", success=False)

            # 带引导语仍没有画图：不再轮询，直接返回上游的文字。
            conversation.conversation_events.reset_mock()
            conversation.conversation_events.side_effect = [analysis, analysis]
            self.backend.resolve_conversation_image_urls.reset_mock()
            outputs = list(conversation.stream_image_outputs(self.backend, request))
            self.assertEqual(conversation.conversation_events.call_count, 2)
            self.assertEqual([(output.kind, output.text) for output in outputs], [("progress", "背景是蓝色"), ("message", "背景是蓝色")])
            self.backend.resolve_conversation_image_urls.assert_not_called()

    def test_web_results_use_bytes_and_keep_format_and_metadata(self) -> None:
        for response_format in (None, "b64_json", "url"):
            with self.subTest(response_format=response_format):
                self.backend.download_image_bytes.return_value = [IMAGE_BYTES[0], b"", IMAGE_BYTES[1]]
                self.backend.download_image_bytes.reset_mock()
                self.save.reset_mock()
                self.save.side_effect = STORED_IMAGES
                request = conversation.ConversationRequest(
                    model="gpt-image-2", prompt=" draw ", base_url=BASE_URL,
                )
                if response_format is not None:
                    request.response_format = response_format
                with (
                    mock.patch.object(base64, "b64decode", side_effect=AssertionError("Web bytes must not be decoded")),
                    mock.patch.object(base64, "b64encode", wraps=base64.b64encode) as encode,
                    mock.patch.object(conversation.time, "time", return_value=123),
                ):
                    outputs = list(conversation.stream_image_outputs(self.backend, request, index=2, total=3))
                self.assertEqual(len(outputs), 1)
                output = outputs[0]
                key = request.response_format
                values = IMAGE_URLS if key == "url" else IMAGE_B64
                self.assertEqual(output.data, [{key: value, "revised_prompt": "draw"} for value in values])
                self.assertEqual(
                    (output.kind, output.model, output.index, output.total, output.created, output.conversation_id),
                    ("result", "gpt-image-2", 2, 3, 123, "synthetic-conversation"),
                )
                self.assertEqual(output.to_chunk()["_conversation_id"], "synthetic-conversation")
                self.assertEqual(self.save.call_args_list, [mock.call(data) for data in IMAGE_BYTES] if key == "url" else [])
                self.assertEqual(encode.call_args_list, [] if key == "url" else [mock.call(data) for data in IMAGE_BYTES])
                self.backend.download_image_bytes.assert_called_once_with(IMAGE_URLS)

    def test_empty_downloads_do_not_emit_success(self) -> None:
        for response_format in ("b64_json", "url"):
            for downloaded in ([], [b""]):
                with self.subTest(response_format=response_format, downloaded=downloaded):
                    self.backend.download_image_bytes.return_value = downloaded
                    request = conversation.ConversationRequest(prompt="draw", response_format=response_format)
                    self.assertEqual(list(conversation.stream_image_outputs(self.backend, request)), [])
        self.save.assert_not_called()

    def test_download_and_later_storage_errors_propagate_without_partial_results(self) -> None:
        for stage in ("download", "storage"):
            for response_format in ("b64_json", "url"):
                with self.subTest(stage=stage, response_format=response_format):
                    failure = RuntimeError("synthetic image failure")
                    self.backend.download_image_bytes.side_effect = failure if stage == "download" else None
                    self.save.reset_mock()
                    self.save.side_effect = [STORED_IMAGES[0], failure]
                    request = conversation.ConversationRequest(prompt="draw", response_format=response_format)
                    outputs = conversation.stream_image_outputs(self.backend, request)
                    if stage == "storage" and response_format == "b64_json":
                        self.assertEqual(list(outputs)[0].data, [
                            {"b64_json": value, "revised_prompt": "draw"} for value in IMAGE_B64
                        ])
                        self.save.assert_not_called()
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            next(outputs)
                        self.assertIs(caught.exception, failure)
                        self.assertEqual(self.save.call_count, 0 if stage == "download" else 2)

    def test_codex_still_decodes_at_boundary_without_reencoding(self) -> None:
        for response_format in (None, "b64_json", "url"):
            with self.subTest(response_format=response_format):
                self.backend.iter_codex_image_response_events.return_value = [{
                    "type": "response.completed", "response": {"output": [
                        {"type": "image_generation_call", "result": "data:image/png;base64," + IMAGE_B64[0]},
                    ]},
                }]
                self.save.reset_mock()
                self.save.side_effect = [STORED_IMAGES[0]]
                request = conversation.ConversationRequest(model="codex-gpt-image-2", prompt="draw", base_url=BASE_URL)
                if response_format is not None:
                    request.response_format = response_format
                with (
                    mock.patch.object(base64, "b64decode", wraps=base64.b64decode) as decode,
                    mock.patch.object(base64, "b64encode", side_effect=AssertionError("Codex must reuse base64")),
                ):
                    outputs = list(conversation.stream_codex_image_outputs(self.backend, request, index=2, total=3))
                self.assertEqual(len(outputs), 1)
                value = IMAGE_URLS[0] if request.response_format == "url" else IMAGE_B64[0]
                self.assertEqual(outputs[0].data, [{request.response_format: value, "revised_prompt": "draw"}])
                self.assertEqual((outputs[0].model, outputs[0].index, outputs[0].total), (request.model, 2, 3))
                decode.assert_called_once_with(IMAGE_B64[0], validate=True)
                if request.response_format == "url":
                    self.save.assert_called_once_with(IMAGE_BYTES[0])
                else:
                    self.save.assert_not_called()


class ImageBytesUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        # Do not construct a session, bootstrap, authenticate, or read account settings.
        self.backend = object.__new__(backend_module.OpenAIBackendAPI)
        self.backend._closed = True
        self.backend.access_token = "synthetic-token"

    def test_chat_passes_bytes_directly_and_preserves_attachment_metadata(self) -> None:
        inputs = [(IMAGE_BYTES[0], "image/jpeg"), (bytearray(IMAGE_BYTES[1]), "image/webp"), (IMAGE_BYTES[0], "image/png")]
        names = ["image_1.jpg", "image_2.webp", "image_3.png"]
        refs = [{
            "file_id": f"file-synthetic-{idx}", "file_name": name, "file_size": len(data),
            "mime_type": mime, "width": idx + 1, "height": idx + 2,
        } for idx, ((data, mime), name) in enumerate(zip(inputs, names), start=1)]
        content = [{"type": "text", "text": "describe "}, *[
            {"type": "image", "data": data, "mime": mime} for data, mime in inputs
        ], {"type": "text", "text": "these"}]
        with (
            mock.patch.object(self.backend, "_upload_image_bytes", side_effect=refs) as upload,
            mock.patch.object(self.backend, "_upload_image", side_effect=AssertionError("legacy upload used")),
            mock.patch.object(base64, "b64encode", side_effect=AssertionError("chat bytes encoded")),
            mock.patch.object(base64, "b64decode", side_effect=AssertionError("chat bytes decoded")),
            mock.patch.object(backend_module, "new_uuid", return_value="synthetic-message"),
        ):
            messages = self.backend._api_messages_to_conversation_messages([{"role": "user", "content": content}])
        self.assertEqual(upload.call_args_list, [mock.call(bytes(data), name) for (data, _), name in zip(inputs, names)])
        self.assertTrue(all(isinstance(call.args[0], bytes) for call in upload.call_args_list))
        self.assertEqual(messages, [{
            "id": "synthetic-message", "author": {"role": "user"},
            "content": {"content_type": "multimodal_text", "parts": [*[
                {"content_type": "image_asset_pointer", "asset_pointer": f"file-service://{ref['file_id']}",
                 "width": ref["width"], "height": ref["height"], "size_bytes": ref["file_size"]}
                for ref in refs
            ], "describe these"]},
            "metadata": {"attachments": [
                {"id": ref["file_id"], "mimeType": ref["mime_type"], "name": ref["file_name"],
                 "size": ref["file_size"], "width": ref["width"], "height": ref["height"]}
                for ref in refs
            ]},
        }])

    def test_chat_image_authentication_gate_remains(self) -> None:
        self.backend.access_token = ""
        with mock.patch.object(self.backend, "_upload_image_bytes") as upload:
            with self.assertRaisesRegex(RuntimeError, "authenticated upstream account required"):
                self.backend._api_messages_to_conversation_messages([{
                    "role": "user", "content": [{"type": "image", "data": IMAGE_BYTES[0]}],
                }])
        upload.assert_not_called()

    def test_legacy_base64_and_path_entries_keep_filename_behavior(self) -> None:
        for source, requested_name, expected_name, is_path in (
            (IMAGE_B64[0], None, "image.png", False),
            ("data:image/png;base64," + IMAGE_B64[0], "custom.png", "custom.png", False),
            ("synthetic/local.webp", None, "local.webp", True),
            ("synthetic/local.webp", "ignored.png", "local.webp", True),
        ):
            with self.subTest(source=source, requested_name=requested_name):
                path = mock.Mock()
                path.exists.return_value = is_path
                path.is_file.return_value = is_path
                path.name = "local.webp"
                path.read_bytes.return_value = IMAGE_BYTES[0]
                with (
                    mock.patch.object(backend_module, "Path", return_value=path),
                    mock.patch.object(self.backend, "_upload_image_bytes", return_value=mock.sentinel.uploaded) as upload,
                    mock.patch.object(base64, "b64decode", wraps=base64.b64decode) as decode,
                ):
                    result = (self.backend._upload_image(source) if requested_name is None else
                              self.backend._upload_image(source, requested_name))
                self.assertIs(result, mock.sentinel.uploaded)
                upload.assert_called_once_with(IMAGE_BYTES[0], expected_name)
                if is_path:
                    decode.assert_not_called()
                    path.read_bytes.assert_called_once_with()
                else:
                    decode.assert_called_once_with(IMAGE_B64[0])
                    path.read_bytes.assert_not_called()

    def test_bytes_upload_keeps_http_steps_metadata_and_errors(self) -> None:
        buffer = BytesIO()
        with backend_module.Image.new("RGB", (2, 3), "white") as image:
            image.save(buffer, format="PNG")
        data = buffer.getvalue()
        self.backend.base_url = BASE_URL
        self.backend.user_agent = "synthetic-agent"
        self.backend._headers = mock.Mock(return_value={"synthetic": "headers"})
        self.backend._deadline_timeout_secs = mock.Mock(side_effect=lambda value: value)
        for failed_step in (None, 0, 1, 2):
            with self.subTest(failed_step=failed_step):
                replies = [mock.Mock(), mock.Mock(), mock.Mock()]
                replies[0].json.return_value = {"upload_url": BASE_URL + "/upload", "file_id": "file-synthetic"}
                operations = mock.Mock()
                operations.post.side_effect = [replies[0], replies[2]]
                operations.put.return_value = replies[1]
                self.backend._post_image_file_request = operations.post
                self.backend.session = SimpleNamespace(put=operations.put)
                failure = RuntimeError("synthetic upload failure")
                checks = [None] * 3
                if failed_step is not None:
                    checks[failed_step] = failure
                with (
                    mock.patch.object(backend_module, "ensure_ok", side_effect=checks) as ensure_ok,
                    mock.patch.object(base64, "b64encode", side_effect=AssertionError("upload bytes encoded")),
                    mock.patch.object(base64, "b64decode", side_effect=AssertionError("upload bytes decoded")),
                ):
                    if failed_step is None:
                        result = self.backend._upload_image_bytes(data, "synthetic.png")
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            self.backend._upload_image_bytes(data, "synthetic.png")
                        self.assertIs(caught.exception, failure)
                paths = ["/backend-api/files", "image_upload", "/backend-api/files/file-synthetic/uploaded"]
                step_count = 3 if failed_step is None else failed_step + 1
                self.assertEqual(ensure_ok.call_args_list, [mock.call(replies[i], paths[i]) for i in range(step_count)])
                self.assertEqual([(call[0], call.args[0]) for call in operations.mock_calls], [
                    ("post", paths[0]), ("put", BASE_URL + "/upload"), ("post", paths[2]),
                ][:step_count])
                self.assertEqual(operations.post.call_args_list[0], mock.call(
                    paths[0], headers={"synthetic": "headers"},
                    json={"file_name": "synthetic.png", "file_size": len(data), "use_case": "multimodal", "width": 2, "height": 3},
                    timeout=60,
                ))
                if failed_step is None:
                    self.assertEqual(result, {
                        "file_id": "file-synthetic", "file_name": "synthetic.png", "file_size": len(data),
                        "mime_type": "image/png", "width": 2, "height": 3,
                    })
                    self.assertIs(operations.put.call_args.kwargs["data"], data)
                    self.assertEqual(operations.put.call_args.kwargs["headers"]["Content-Type"], "image/png")
                    self.assertEqual(operations.put.call_args.kwargs["timeout"], 120)
                    self.assertEqual(operations.post.call_args_list[1], mock.call(
                        paths[2], headers={"synthetic": "headers"}, data="{}", timeout=60,
                    ))


class ImagePatchTokenTests(unittest.TestCase):
    def test_public_patch_counting_is_unchanged(self) -> None:
        for model in ("gpt-5.4-mini", "gpt-4o"):
            self.assertEqual(image_tokens.count_image_input_tokens(1024, 1024, model), 1659)
            self.assertEqual(image_tokens.count_image_input_tokens(1024, 1024, model, detail="low"), 415)
            self.assertEqual(image_tokens.count_image_input_tokens(0, 1024, model), 0)
        self.assertEqual(image_tokens.count_image_output_tokens("1024x1024", "high", count=2), 8320)


if __name__ == "__main__":
    unittest.main()
