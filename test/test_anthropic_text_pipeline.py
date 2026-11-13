from __future__ import annotations

import unittest
from unittest import mock

from services.protocol import anthropic_v1_messages as anthropic
from services.protocol import conversation


class AnthropicTextPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = {
            "model": "synthetic-model",
            "messages": [{"role": "user", "content": "synthetic prompt"}],
            "thinking_effort": "high",
        }
        self.request = anthropic.MessageRequest(
            backend=object(),
            model=self.body["model"],
            messages=self.body["messages"],
        )
        self.conversation_request = conversation.ConversationRequest(
            model=self.request.model, messages=self.request.messages,
        )

    def test_non_stream_collects_raw_text_with_default_thinking(self) -> None:
        with (
            mock.patch.object(anthropic, "message_request", return_value=self.request),
            mock.patch.object(conversation, "stream_text_deltas", return_value=iter(["Hello", "", " world"])) as source,
            mock.patch.object(anthropic, "collect_text", wraps=conversation.collect_text) as collect,
            mock.patch.object(anthropic, "count_message_tokens", return_value=5) as input_tokens,
            mock.patch.object(anthropic, "count_text_tokens", return_value=7) as output_tokens,
        ):
            response = anthropic.handle(self.body)

        collect.assert_called_once_with(self.request.backend, self.conversation_request)
        source.assert_called_once_with(self.request.backend, self.conversation_request)
        self.assertEqual(source.call_args.args[1].thinking_effort, "")
        input_tokens.assert_called_once_with(self.request.messages, self.request.model)
        output_tokens.assert_called_once_with("Hello world", self.request.model)
        self.assertTrue(response["id"].startswith("msg_"))
        self.assertEqual(response, {
            "id": response["id"],
            "type": "message",
            "role": "assistant",
            "model": self.request.model,
            "content": [{"type": "text", "text": "Hello world"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 7},
        })

    def test_stream_finishes_on_normal_exhaustion_including_empty_text(self) -> None:
        for deltas in (["Hello", "", " world"], []):
            with self.subTest(deltas=deltas):
                text = "".join(deltas)
                with (
                    mock.patch.object(anthropic, "message_request", return_value=self.request),
                    mock.patch.object(anthropic, "stream_text_deltas", return_value=iter(deltas)) as source,
                    mock.patch.object(anthropic, "count_message_tokens", return_value=5) as input_tokens,
                    mock.patch.object(anthropic, "count_text_tokens", return_value=len(text)) as output_tokens,
                ):
                    events = list(anthropic.handle({**self.body, "stream": True}))

                source.assert_called_once_with(self.request.backend, self.conversation_request)
                self.assertEqual(source.call_args.args[1].thinking_effort, "")
                input_tokens.assert_called_once_with(self.request.messages, self.request.model)
                output_tokens.assert_called_once_with(text, self.request.model)
                self.assertTrue(events[0]["message"]["id"].startswith("msg_"))
                self.assertEqual(events[0], {
                    "type": "message_start",
                    "message": {
                        "id": events[0]["message"]["id"], "type": "message", "role": "assistant",
                        "model": self.request.model, "content": [], "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 5, "output_tokens": 0},
                    },
                })
                self.assertEqual(events[1:-1], [
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    *[
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}}
                        for delta in deltas if delta
                    ],
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": len(text)}},
                ])
                self.assertEqual(events[-1], {"type": "message_stop", "created": events[-1]["created"]})
                self.assertIsInstance(events[-1]["created"], int)

    def test_tool_stream_preserves_xml_arguments_block_order_and_raw_usage(self) -> None:
        xml_deltas = [
            "<tool_calls><tool_call><tool_name>lookup</tool_name><parameters>",
            "<query><![CDATA[合成]]></query><limit>2</limit>",
            "</parameters></tool_call></tool_calls>",
        ]
        tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
        for prefix in ("", "Preface"):
            with self.subTest(prefix=prefix):
                deltas = [prefix, *xml_deltas]
                output_tokens = mock.Mock(return_value=9)
                events = list(anthropic.stream_events(iter(deltas), self.request.model, 5, output_tokens, tools))

                output_tokens.assert_called_once_with("".join(deltas))
                self.assertEqual(events[0]["type"], "message_start")
                self.assertEqual(events[-1]["type"], "message_stop")
                tool_id = events[-5]["content_block"]["id"]
                self.assertTrue(tool_id.startswith("toolu_"))
                index = 1 if prefix else 0
                text_events = [
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": prefix}},
                    {"type": "content_block_stop", "index": 0},
                ] if prefix else []
                self.assertEqual(events[1:-1], [
                    *text_events,
                    {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": tool_id, "name": "lookup", "input": {}}},
                    {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": '{"query": "合成", "limit": 2}'}},
                    {"type": "content_block_stop", "index": index},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 9}},
                ])

    def test_stream_error_propagates_without_success_finalization(self) -> None:
        for tools in (None, [{"name": "lookup"}]):
            with self.subTest(tools=tools):
                error = RuntimeError("synthetic delta failure")

                def failing_deltas():
                    yield "partial"
                    if tools:
                        yield "<tool_calls><tool_call><tool_name>lookup</tool_name><parameters>{}</parameters></tool_call></tool_calls>"
                    raise error

                output_tokens = mock.Mock()
                received = []
                with self.assertRaises(RuntimeError) as caught:
                    received.extend(anthropic.stream_events(failing_deltas(), self.request.model, 5, output_tokens, tools))

                self.assertIs(caught.exception, error)
                self.assertEqual([event["type"] for event in received], [
                    "message_start", "content_block_start", "content_block_delta",
                ])
                self.assertEqual(received[-1]["delta"], {"type": "text_delta", "text": "partial"})
                output_tokens.assert_not_called()


if __name__ == "__main__":
    unittest.main()
