from __future__ import annotations

import hashlib
import json
import itertools
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from services.config import DATA_DIR
from services.protocol.error_response import anthropic_error_response, error_message_from_detail, openai_error_response
from utils.helper import anthropic_sse_stream, sse_json_stream

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"
INTERNAL_RESPONSE_KEYS = {"_account_email", "_conversation_id"}


class LogService:
    # The production singleton serializes file operations across threads.
    # Its instance lock does not coordinate other instances or processes.

    def __init__(self, path: Path, *, max_bytes: int = 16 * 1024 * 1024,
                 backup_count: int = 5, block_size: int = 64 * 1024,
                 max_line_bytes: int = 1024 * 1024):
        if max_bytes <= 0 or backup_count < 0 or block_size <= 0 or max_line_bytes <= 0:
            raise ValueError("Invalid log rotation/read limits")
        self.path = Path(path).resolve()
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.block_size = block_size
        # Oversized lines remain on disk but are not decoded or returned.
        self.max_line_bytes = max_line_bytes
        # Reserve space for the 32-byte generated ID field when rewriting a
        # legacy line right at the limit, so deletion cannot hide that record.
        self._line_limit = max_line_bytes + 32
        self._lock = RLock()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _file_identity(file: BinaryIO) -> tuple[int, int]:
        stat = os.fstat(file.fileno())
        return stat.st_dev, stat.st_ino

    @staticmethod
    def _has_id(item: dict[str, Any]) -> bool:
        return item.get("id") is not None and item["id"] != ""

    @staticmethod
    def _legacy_id(raw_line: bytes, offset: int, identity: tuple[int, int]) -> str:
        # Rename preserves file identity. Offset distinguishes identical lines
        # without counting preceding lines (including in a pre-existing huge log).
        digest = hashlib.sha256(f"{identity[0]}:{identity[1]}:{offset}:".encode("ascii"))
        digest.update(raw_line.rstrip(b"\r\n"))
        return digest.hexdigest()[:24]

    def _item_id(self, item: dict[str, Any], raw_line: bytes, offset: int, identity: tuple[int, int]) -> str:
        if self._has_id(item):
            return str(item["id"])
        return self._legacy_id(raw_line, offset, identity)

    def _parse_line(self, raw_line: bytes) -> dict[str, Any] | None:
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(item, dict):
            return None
        if len(raw_line.rstrip(b"\n")) > self.max_line_bytes and not self._has_id(item):
            return None
        return item

    @staticmethod
    def _serialize_item(item: dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _matches_filters(item: dict[str, Any], *, type: str = "", start_date: str = "", end_date: str = "") -> bool:
        t = str(item.get("time") or "")
        day = t[:10]
        if type and item.get("type") != type:
            return False
        if start_date and day < start_date:
            return False
        if end_date and day > end_date:
            return False
        return True

    def _backup_path(self, number: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{number}")

    def _paths(self) -> Iterator[Path]:
        # Physical append order, rather than timestamps supplied by old writers.
        yield self.path
        for number in range(1, self.backup_count + 1):
            yield self._backup_path(number)

    def _reverse_lines(self, file: BinaryIO) -> Iterator[tuple[int, bytes]]:
        position = file.seek(0, os.SEEK_END)
        parts: list[bytes] = []
        size = 0
        oversized = False
        while position:
            start = max(0, position - self.block_size)
            file.seek(start)
            block = file.read(position - start)
            end = len(block)
            while True:
                newline = block.rfind(b"\n", 0, end)
                begin = newline + 1
                if not oversized:
                    size += end - begin
                    if size > self._line_limit:
                        parts.clear()
                        oversized = True
                    else:
                        parts.append(block[begin:end])
                if newline < 0:
                    break
                if not oversized:
                    yield start + begin, b"".join(reversed(parts))
                parts.clear()
                size = 0
                oversized = False
                end = newline
            position = start
        if not oversized:
            yield 0, b"".join(reversed(parts))

    def _rotate(self) -> None:
        # Move even a legacy 98 MiB file intact: no full scan, ID migration or
        # lossy splitting on the first append. It ages out with normal retention.
        if not self.backup_count:
            self.path.unlink(missing_ok=True)
            return
        self._backup_path(self.backup_count).unlink(missing_ok=True)
        for number in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(number)
            if source.exists():
                os.replace(source, self._backup_path(number + 1))
        os.replace(self.path, self._backup_path(1))

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        item = {
            "id": uuid4().hex,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": type,
            "summary": summary,
            "detail": detail or data,
        }
        payload = (self._serialize_item(item) + "\n").encode("utf-8")
        with self._lock:
            size = 0
            separator = b""
            try:
                with self.path.open("rb") as file:
                    size = file.seek(0, os.SEEK_END)
                    if size:
                        file.seek(-1, os.SEEK_END)
                        if file.read(1) != b"\n":
                            separator = b"\n"
            except FileNotFoundError:
                pass
            if size and size + len(separator) + len(payload) > self.max_bytes:
                self._rotate()
                separator = b""
            # A single oversized record is kept whole; the next add rotates it.
            # Separate a valid unterminated record or a torn write from this one.
            with self.path.open("ab") as file:
                file.write(separator)
                file.write(payload)

    def list(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items: list[dict[str, Any]] = []
        with self._lock:
            for path in self._paths():
                try:
                    file = path.open("rb")
                except FileNotFoundError:
                    continue
                # The caller owns the handle, so an early return closes it even
                # while the reverse-line generator is suspended at a yield.
                with file:
                    identity = self._file_identity(file)
                    for offset, raw_line in self._reverse_lines(file):
                        item = self._parse_line(raw_line)
                        if item is None or not self._matches_filters(
                            item, type=type, start_date=start_date, end_date=end_date,
                        ):
                            continue
                        item["id"] = self._item_id(item, raw_line, offset, identity)
                        items.append(item)
                        if len(items) >= limit:
                            return items
        return items

    def _delete_from_file(self, path: Path, target_ids: set[str]) -> int:
        temporary: Path | None = None
        removed = 0
        try:
            with path.open("rb") as source:
                identity = self._file_identity(source)
                with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                                 prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    while True:
                        offset = source.tell()
                        raw_line = source.readline(self._line_limit + 2)
                        if not raw_line:
                            break
                        if len(raw_line.rstrip(b"\n")) > self._line_limit:
                            # Never buffer an unbounded line, nor interpret its
                            # later chunks as separate records. Preserve every byte.
                            output.write(raw_line)
                            while not raw_line.endswith(b"\n"):
                                raw_line = source.readline(self.block_size)
                                if not raw_line:
                                    break
                                output.write(raw_line)
                            continue
                        item = self._parse_line(raw_line)
                        if item is not None:
                            item_id = self._item_id(item, raw_line, offset, identity)
                            if item_id in target_ids:
                                removed += 1
                                continue
                            if not self._has_id(item):
                                # Replacement changes inode/offsets, so persist ALL
                                # retained legacy IDs. Insert only an ID, leaving
                                # original fields, numeric spellings and whitespace
                                # intact instead of reserializing the whole object.
                                end = raw_line.rfind(b"}")
                                field = (b"," if item else b"") + b'"id":' + json.dumps(item_id).encode("ascii")
                                raw_line = raw_line[:end] + field + raw_line[end:]
                        output.write(raw_line)
                    if removed:
                        output.flush()
                        os.fsync(output.fileno())
            # Both handles must be closed before replace, notably on Windows.
            if removed:
                os.replace(temporary, path)
            return removed
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def delete(self, ids: list[str]) -> dict[str, int]:
        target_ids = {str(item or "").strip() for item in ids if str(item or "").strip()}
        if not target_ids:
            return {"removed": 0}
        with self._lock:
            removed = 0
            for path in self._paths():
                if path.exists():
                    removed += self._delete_from_file(path, target_ids)
            return {"removed": removed}


log_service = LogService(DATA_DIR / "logs.jsonl")


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key == "urls" and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _collect_account_emails(value: object) -> list[str]:
    emails: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"_account_email", "account_email"} and isinstance(item, str) and item.strip():
                emails.append(item.strip())
            else:
                emails.extend(_collect_account_emails(item))
    elif isinstance(value, list):
        for item in value:
            emails.extend(_collect_account_emails(item))
    return emails


def _collect_conversation_ids(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "_conversation_id" and isinstance(item, str) and item.strip():
                ids.append(item.strip())
            else:
                ids.extend(_collect_conversation_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.extend(_collect_conversation_ids(item))
    return ids


def _strip_internal_response_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_internal_response_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_RESPONSE_KEYS
        }
    if isinstance(value, list):
        return [_strip_internal_response_fields(item) for item in value]
    return value


def _request_excerpt(text: object, limit: int = 1000) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _image_error_response(exc: Exception) -> JSONResponse:
    from services.protocol.conversation import public_image_error_message

    message = public_image_error_message(str(exc))
    if "no available image quota" in message.lower():
        return openai_error_response(
            {
                "error": {
                    "message": "no available image quota",
                    "type": "insufficient_quota",
                    "param": None,
                    "code": "insufficient_quota",
                }
            },
            429,
        )
    if hasattr(exc, "to_openai_error") and hasattr(exc, "status_code"):
        return JSONResponse(status_code=int(exc.status_code), content=exc.to_openai_error())
    return openai_error_response(message, 502)


def _protocol_error_response(exc: Exception, status_code: int, sse: str) -> JSONResponse:
    message = str(exc)
    if sse == "anthropic":
        return anthropic_error_response(message, status_code)
    return openai_error_response(message, status_code)


def _next_item(items):
    try:
        return True, next(items)
    except StopIteration:
        return False, None


@dataclass
class LoggedCall:
    identity: dict[str, object]
    endpoint: str
    model: str
    summary: str
    started: float = field(default_factory=time.time)
    request_text: str = ""
    request_shape: dict[str, int] | None = None

    async def run(self, handler, *args, sse: str = "openai"):
        from services.protocol.conversation import ImageGenerationError

        try:
            result = await run_in_threadpool(handler, *args)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)

        if isinstance(result, dict):
            self.log("调用完成", result)
            response = dict(result)
            response.pop("_account_email", None)
            return response

        sender = anthropic_sse_stream if sse == "anthropic" else sse_json_stream
        try:
            has_first, first = await run_in_threadpool(_next_item, result)
        except ImageGenerationError as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""),
                     conversation_id=getattr(exc, "conversation_id", ""))
            return _image_error_response(exc)
        except HTTPException as exc:
            self.log("调用失败", status="failed", error=str(exc.detail))
            raise
        except Exception as exc:
            self.log("调用失败", status="failed", error=str(exc), account_email=getattr(exc, "account_email", ""))
            if self.endpoint.startswith("/v1/images"):
                return _image_error_response(exc)
            return _protocol_error_response(exc, 502, sse)
        if not has_first:
            self.log("流式调用结束")
            return StreamingResponse(sender(()), media_type="text/event-stream")
        return StreamingResponse(sender(self.stream(itertools.chain([first], result))), media_type="text/event-stream")

    def stream(self, items):
        urls: list[str] = []
        account_emails: list[str] = []
        conversation_ids: list[str] = []
        failed = False
        try:
            for item in items:
                urls.extend(_collect_urls(item))
                account_emails.extend(_collect_account_emails(item))
                conversation_ids.extend(_collect_conversation_ids(item))
                if isinstance(item, dict) and item.get("type") == "response.failed":
                    failed = True
                    response = item.get("response")
                    error = response.get("error") if isinstance(response, dict) else None
                    self.log(
                        "流式调用失败", status="failed",
                        error=error_message_from_detail(error) or "response failed",
                        urls=urls,
                        account_email=account_emails[0] if account_emails else "",
                        conversation_id=conversation_ids[0] if conversation_ids else "",
                    )
                yield _strip_internal_response_fields(item)
        except Exception as exc:
            failed = True
            self.log(
                "流式调用失败",
                status="failed",
                error=str(exc),
                urls=urls,
                account_email=(account_emails[0] if account_emails else getattr(exc, "account_email", "")),
                conversation_id=(conversation_ids[0] if conversation_ids else getattr(exc, "conversation_id", "")),
            )
            if self.endpoint.startswith("/v1/images") and not hasattr(exc, "to_openai_error"):
                from services.protocol.conversation import ImageGenerationError, public_image_error_message

                raise ImageGenerationError(public_image_error_message(str(exc))) from exc
            raise
        finally:
            if not failed:
                self.log("流式调用结束", urls=urls, account_email=account_emails[0] if account_emails else "",
                         conversation_id=conversation_ids[0] if conversation_ids else "")

    def log(self, suffix: str, result: object = None, status: str = "success", error: str = "",
            urls: list[str] | None = None, account_email: str = "", conversation_id: str = "") -> None:
        detail = {
            "key_id": self.identity.get("id"),
            "key_name": self.identity.get("name"),
            "role": self.identity.get("role"),
            "endpoint": self.endpoint,
            "model": self.model,
            "started_at": datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S"),
            "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": int((time.time() - self.started) * 1000),
            "status": status,
        }
        request_excerpt = _request_excerpt(self.request_text)
        if request_excerpt:
            detail["request_text"] = request_excerpt
        if self.request_shape:
            detail["request_shape"] = self.request_shape
        if error:
            detail["error"] = error
        email = str(account_email or "").strip()
        if not email:
            emails = _collect_account_emails(result)
            email = emails[0] if emails else ""
        if email:
            detail["account_email"] = email
        conv_id = str(conversation_id or "").strip()
        if not conv_id:
            conv_ids = _collect_conversation_ids(result)
            conv_id = conv_ids[0] if conv_ids else ""
        if conv_id:
            detail["conversation_id"] = conv_id
        collected_urls = [*(urls or []), *_collect_urls(result)]
        if collected_urls and not self.endpoint.startswith("/v1/search"):
            detail["urls"] = list(dict.fromkeys(collected_urls))
        log_service.add(LOG_TYPE_CALL, f"{self.summary}{suffix}", detail)
