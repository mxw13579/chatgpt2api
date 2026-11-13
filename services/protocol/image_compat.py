"""Shared image request adaptation with proxy-aware reference fetching."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from fastapi import HTTPException

from services.config import config
from services.protocol import conversation
from services.protocol.conversation import ImageGenerationError, ImageOutput
from utils.helper import (
    MAX_JSON_EDIT_IMAGES,
    extract_image_from_message_content,
    extract_prompt_from_message_content,
    is_explicit_image_model,
    is_image_chat_request,
    is_supported_image_model,
    normalize_json_edit_images,
)

__all__ = [
    "NormalizedImageRequest",
    "image_request_prompt",
    "is_image_request",
    "normalize_image_request",
    "require_image_outputs",
]

_IMAGE_PART_TYPES = {"image_url", "input_image", "image"}
_CONTENT_PART_TYPES = {"text", "input_text", *_IMAGE_PART_TYPES}


@dataclass
class NormalizedImageRequest:
    request: conversation.ConversationRequest
    images: list[tuple[bytes, str, str]]


def is_image_request(body: dict) -> bool:
    # Detection must not hide conflicts such as an image model + tool_choice=none.
    return is_image_chat_request(body)


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": message})


def _normalized_name(value: object) -> str:
    return str(value or "").strip().lower()


def _image_tool(body: dict) -> dict:
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and _normalized_name(tool.get("type")) == "image_generation":
                return tool
    return {}


def _validate_tool_choice(body: dict) -> None:
    choice = body.get("tool_choice")
    if choice is None:
        return
    if isinstance(choice, str) and choice.strip().lower() in {"auto", "required"}:
        return
    if isinstance(choice, dict) and _normalized_name(choice.get("type")) == "image_generation":
        return
    raise _bad_request("tool_choice conflicts with image generation")


def _image_model(body: dict, tool: dict) -> str:
    model = _normalized_name(body.get("model"))
    tool_model = _normalized_name(tool.get("model"))
    if tool_model and not is_supported_image_model(tool_model):
        raise _bad_request("unsupported image_generation tool.model")
    if is_explicit_image_model(model):
        if tool_model and tool_model != model:
            raise _bad_request("image model conflicts with image_generation tool.model")
        if not is_supported_image_model(model):
            raise _bad_request("unsupported image model")
        return model
    if tool_model:
        return tool_model
    if model != "auto" and is_supported_image_model(model):
        return model
    default_model = _normalized_name(config.default_image_model)
    if not is_supported_image_model(default_model):
        raise _bad_request("unsupported default image model")
    return default_model


def _user_contents(source: object) -> list[object]:
    """Keep message boundaries; a bare content-block array is one user turn."""
    if isinstance(source, str):
        return [source]
    items = source if isinstance(source, list) else [source]
    contents: list[object] = []
    blocks: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = _normalized_name(item.get("role"))
        if _normalized_name(item.get("type")) in _CONTENT_PART_TYPES:
            if not role or role == "user":
                blocks.append(item)
            continue
        if blocks:
            contents.append(blocks)
            blocks = []
        if role == "user":
            contents.append(item.get("content"))
    if blocks:
        contents.append(blocks)
    return contents


def _request_contents(body: dict, *, protocol: str) -> list[object]:
    if protocol not in {"chat", "responses"}:
        raise ValueError("protocol must be chat or responses")
    native, fallback = ("messages", "input") if protocol == "chat" else ("input", "messages")
    # Only a missing/null native field permits fallback, never merge both sources.
    source = body.get(native)
    if source is None:
        source = body.get(fallback)
    return _user_contents(source)


def image_request_prompt(body: dict, *, protocol: str) -> str:
    """Extract the final prompt without decoding/fetching images or accessing accounts."""
    contents = _request_contents(body, protocol=protocol)
    direct_prompt = body.get("prompt")
    if direct_prompt is not None and not isinstance(direct_prompt, str):
        raise _bad_request("prompt must be a string")
    prompt = (direct_prompt or "").strip()
    if not prompt:
        prompt = "\n".join(filter(None, (extract_prompt_from_message_content(content) for content in contents))).strip()
    if not prompt:
        raise _bad_request("prompt is required")
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise _bad_request("instructions must be a string")
    if instructions and instructions.strip():
        prompt = f"{instructions.strip()}\n\n{prompt}"
    return prompt


def _has_file_id(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return value.get("file_id") is not None or any(
        _has_file_id(value.get(key)) for key in ("image_url", "source")
    )


def _reference_image_entry(part: dict) -> dict:
    """Adapt legacy reference shapes to the strict JSON validator's input."""
    filename = str(part.get("filename") or part.get("file_name") or "").strip() or None
    mime_type = str(part.get("mime_type") or part.get("mimeType") or part.get("mime") or "").strip() or None
    value = part.get("b64_json") or part.get("base64")
    if not value:
        image_url = part.get("image_url") or part.get("url")
        if isinstance(image_url, dict):
            filename = filename or str(image_url.get("filename") or image_url.get("file_name") or "").strip() or None
            mime_type = mime_type or str(image_url.get("mime_type") or image_url.get("mimeType") or "").strip() or None
            value = image_url.get("url") or image_url.get("image_url")
        else:
            value = image_url
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            # Reuse the existing proxy-aware downloader and propagate its failures.
            downloaded = extract_image_from_message_content([
                {"type": "image_url", "image_url": value.strip()},
            ])
            if len(downloaded) != 1:
                raise _bad_request("image entry must include image data")
            data, mime_type = downloaded[0]
            if not data:
                raise _bad_request("image file is empty")
            # The downloaded MIME is authoritative, just like a data URL's MIME.
            value = base64.b64encode(data).decode("ascii")
    if not value:
        data = part.get("data")
        source = part.get("source")
        if isinstance(data, (bytes, bytearray)):
            if not data:
                raise _bad_request("image file is empty")
            value = base64.b64encode(data).decode("ascii")
            mime_type = part.get("mime") or mime_type
        elif isinstance(source, dict) and _normalized_name(source.get("type")) == "base64":
            value = source.get("data")
            mime_type = source.get("media_type") or source.get("mime_type") or mime_type
            filename = filename or source.get("filename") or source.get("file_name")
    return {"b64_json": value, "filename": filename, "mime_type": mime_type}


def _reference_images(contents: list[object]) -> list[tuple[bytes, str, str]]:
    latest: list[dict] = []
    for content in contents:
        if not isinstance(content, list):
            continue
        parts = [
            part for part in content
            if isinstance(part, dict) and _normalized_name(part.get("type")) in _IMAGE_PART_TYPES
        ]
        if parts:
            latest = parts
    for part in latest:
        if _has_file_id(part):
            raise _bad_request("file_id image references are not supported; use base64 image data")
    if len(latest) > MAX_JSON_EDIT_IMAGES:
        raise _bad_request(f"images supports up to {MAX_JSON_EDIT_IMAGES} items")
    # Count before any downloads; then validate every local/downloaded image alike.
    # A malformed selected image must fail, not silently turn an edit into generation.
    entries = [_reference_image_entry(part) for part in latest]
    return normalize_json_edit_images(images=entries) if entries else []


def _option(body: dict, tool: dict, name: str, default: object = None) -> object:
    if body.get(name) is not None:
        return body[name]
    value = tool.get(name)
    return default if value is None else value


def _validate_output_options(body: dict, tool: dict) -> None:
    # The adapters emit PNG/base64 final results, not JPEG or partial-image previews.
    for source in (body, tool):
        output_format = source.get("output_format")
        if output_format is not None and _normalized_name(output_format) != "png":
            raise _bad_request("only PNG image output is supported")
        if source.get("partial_images") not in (None, 0):
            raise _bad_request("partial_images previews are not supported")


def normalize_image_request(body: dict, *, protocol: str) -> NormalizedImageRequest:
    contents = _request_contents(body, protocol=protocol)
    _validate_tool_choice(body)
    tool = _image_tool(body)
    model = _image_model(body, tool)
    _validate_output_options(body, tool)

    prompt = image_request_prompt(body, protocol=protocol)

    n = _option(body, tool, "n", 1)
    if type(n) is not int:
        raise _bad_request("n must be an integer")
    if not 1 <= n <= 4:
        raise _bad_request("n must be between 1 and 4")
    size = _option(body, tool, "size")
    if size is not None:
        if not isinstance(size, str) or not size.strip():
            raise _bad_request("size must be a non-empty string or null")
        size = size.strip().lower()
        if size != "auto" and not re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size):
            raise _bad_request("size must be auto or WIDTHxHEIGHT")
    quality = _option(body, tool, "quality", "auto")
    if not isinstance(quality, str):
        raise _bad_request("quality must be a string")
    quality = quality.strip().lower() or "auto"
    if quality not in {"auto", "low", "medium", "high", "xhigh", "max", "standard", "hd"}:
        raise _bad_request("unsupported image quality")
    images = _reference_images(contents)
    return NormalizedImageRequest(
        request=conversation.ConversationRequest(
            model=model,
            prompt=prompt,
            images=conversation.encode_images(images) or None,
            n=n,
            size=size,
            quality=quality,
            response_format="b64_json",
            message_as_error=True,
            strict_image_results=True,
        ),
        images=images,
    )


def require_image_outputs(
    outputs: Iterable[ImageOutput], *, expected_count: int = 1,
) -> Iterator[ImageOutput]:
    """Check inline results and completion; bytes were validated upstream."""
    count = 0
    for output in outputs:
        if output.kind == "message":
            # Do not surface untrusted upstream text or account metadata in errors.
            raise ImageGenerationError("Image generation returned text instead of an image.", code="no_image_generated")
        if output.kind == "progress":
            yield output
        elif output.kind == "result":
            if not output.data or any(not item.get("b64_json") for item in output.data):
                raise ImageGenerationError("Image generation returned no inline image data.", code="no_image_generated")
            count += len(output.data)
            yield output
    if not count:
        raise ImageGenerationError("Image generation returned no images.", code="no_image_generated")
    if count < expected_count:
        raise ImageGenerationError(
            f"Image generation returned {count} of {expected_count} requested images.",
            code="incomplete_image_generation",
        )
