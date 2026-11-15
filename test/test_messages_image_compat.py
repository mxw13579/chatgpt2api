"""Image models on /v1/messages; the image pool and tokenization are mocked."""
from __future__ import annotations

import unittest
from unittest import mock

from services.protocol import anthropic_v1_messages as messages
from services.protocol.conversation import ImageOutput

MODEL = "gpt-image-2"
PNG_B64 = "iVBORw0KGgo="
BODY = {
    "model": MODEL,
    "system": "Synthetic system prompt.",
    "tool_choice": {"type": "auto"},
    "messages": [
        {"role": "user", "content": "Synthetic earlier turn."},
        {"role": "assistant", "content": "Synthetic assistant turn."},
        {"role": "user", "content": [{"type": "text", "text": "Draw a synthetic square."}]},
    ],
}


def output(kind: str, **kwargs) -> ImageOutput:
    return ImageOutput(kind=kind, model=MODEL, index=1, total=1, **kwargs)


class MessagesImageCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, kwargs in (
            ("stream_image_outputs_with_pool", {}),
            ("count_text_tokens", {"return_value": 3}),
            ("count_image_output_items_tokens", {"return_value": 5}),
            ("message_request", {"side_effect": AssertionError("text path must not run")}),
        ):
            patcher = mock.patch.object(messages, name, **kwargs)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def test_image_is_returned_as_markdown_from_the_latest_user_turn(self) -> None:
        self.stream_image_outputs_with_pool.return_value = iter([output("result", data=[{"b64_json": PNG_B64}])])
        response = messages.handle(dict(BODY))

        self.assertEqual(self.stream_image_outputs_with_pool.call_args.args[0].prompt, "Draw a synthetic square.")
        self.assertEqual(response["content"], [{"type": "text", "text": f"![image_1](data:image/png;base64,{PNG_B64})"}])
        self.assertEqual(response["usage"], {"input_tokens": 3, "output_tokens": 5})

    def test_stream_and_text_reply(self) -> None:
        self.stream_image_outputs_with_pool.return_value = iter([output("message", text="Synthetic text reply.")])
        events = list(messages.handle({**BODY, "stream": True}))

        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(
            "".join(event["delta"]["text"] for event in events if event["type"] == "content_block_delta"),
            "Synthetic text reply.",
        )
        self.assertEqual(events[-1]["type"], "message_stop")


if __name__ == "__main__":
    unittest.main()
