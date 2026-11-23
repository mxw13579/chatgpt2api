import base64
import json
import os
import random
import re
import threading
import time

import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from curl_cffi import requests
from PIL import Image

from services.account_service import account_service
from services.config import config
from services.proxy_service import proxy_settings
from utils.helper import UpstreamHTTPError, ensure_ok, iter_sse_payloads, new_uuid, split_image_model
from utils.log import logger
from utils.pow import build_legacy_requirements_token, build_proof_token, parse_pow_resources
from utils.turnstile import solve_turnstile_token


class InvalidAccessTokenError(RuntimeError):
    pass


class ImageTaskError(RuntimeError):
    """图片生成异常基类，携带上游会话 ID 供调用方清理对话。"""

    def __init__(self, message: str = "", conversation_id: str = "") -> None:
        super().__init__(message)
        self.conversation_id = conversation_id


class ImagePollTimeoutError(ImageTaskError):
    pass


class ImageContentPolicyError(ImageTaskError):
    """Raised when image generation is blocked by content policy moderation."""
    pass


class ImageQueuedError(ImageTaskError):
    """上游把这次生图排进了免费队列，短时间内不会出图。"""
    pass


class ImageQuotaExceededError(ImageTaskError):
    """账号的图像生成额度已用完。"""
    pass


class ImageSourceMissingError(ImageTaskError):
    """上游图像工具把请求当成编辑已有图片，但对话里没有可用的原图。"""
    pass


class ImageToolFailedError(ImageTaskError):
    """上游图像工具自己报错，或这个账号当前不能画图：这一轮不会出图，换账号可能成功。"""
    pass


class ImageStreamHardTimeoutError(RuntimeError):
    """图片 SSE 流读取超过硬上限时抛出，用于快速中断被挂起的长连接。"""
    pass


@dataclass
class ChatRequirements:
    """保存一次对话请求所需的 sentinel token。"""
    token: str
    proof_token: str = ""
    turnstile_token: str = ""
    so_token: str = ""
    raw_finalize: Optional[Dict[str, Any]] = None


DEFAULT_CLIENT_VERSION = "prod-a194cd50d4416d3c0b47c740f206b12ce60f5887"
DEFAULT_CLIENT_BUILD_NUMBER = "6708908"
DEFAULT_POW_SCRIPT = "https://chatgpt.com/backend-api/sentinel/sdk.js"
CODEX_IMAGE_MODEL = "codex-gpt-image-2"
CODEX_RESPONSES_MODEL = "gpt-5.5"
TEXT_STREAM_TIMEOUT_SECS = 300.0
IMAGE_FILE_THROTTLE_RETRIES = 3
IMAGE_FILE_THROTTLE_BACKOFF_SECS = (1.0, 2.0, 4.0)
SEARCH_MODEL = "gpt-5-5"
SEARCH_TIMEOUT_SECS = 300.0
SEARCH_POLL_INTERVAL_SECS = 3.0
SEARCH_DONE_STATUS = {"finished_successfully", "finished_partial_completion"}
SEARCH_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([^"]+)"')
SEARCH_URL_RE = re.compile(r"https?://[^\s\"'<>）)\]}]+")
FILE_SERVICE_ID_RE = re.compile(r"file-service://([A-Za-z0-9_-]+)")
# 真正的图片文件 ID 格式：file_00000000 + 24位十六进制字符（共32字符）
REAL_IMAGE_FILE_ID_RE = re.compile(r"\bfile_00000000[a-f0-9]{24}\b")
SEDIMENT_ID_RE = re.compile(r"sediment://([A-Za-z0-9_-]+)")
CODEX_RESPONSES_INSTRUCTIONS = (
    "Use the image_generation tool to create exactly one image for the user's request. "
    "Return the generated image result."
)

# 内容政策违规错误关键词（上游拒绝生成图片的各种表述）
_CONTENT_POLICY_KEYWORDS = (
    # 明确的内容政策违规
    "内容政策", "防护限制", "违反", "moderation", "policy", "blocked",
    # 拒绝生成类
    "不能生成", "无法生成", "不能帮助", "无法帮助",
    # 敏感内容类
    "裸体", "裸露", "色情", "性内容", "未成年",
    # 通用拒绝
    "抱歉，我不能",
)


def _is_content_policy_error(error_msg: str) -> bool:
    """检查错误消息是否为内容政策违规。"""
    if not error_msg:
        return False
    msg_lower = error_msg.lower()
    return any(keyword in msg_lower for keyword in _CONTENT_POLICY_KEYWORDS)


class OpenAIBackendAPI:
    """ChatGPT Web 后端封装。

    说明：
    - 传入 `access_token` 时，聊天和模型列表都会走已登录链路
      例如 `/backend-api/sentinel/chat-requirements`、`/backend-api/conversation`
    - 不传 `access_token` 时，会走未登录链路
      例如 `/backend-anon/sentinel/chat-requirements`、`/backend-anon/conversation`
    - `stream_conversation()` 是底层统一流式入口
    - 协议兼容转换放在 `services.protocol`
    """

    def __init__(self, access_token: str = "") -> None:
        """初始化后端客户端。

        参数：
        - `access_token`：可选。传入后表示使用已登录链路；不传则使用未登录链路。
        """
        self.base_url = "https://chatgpt.com"
        self.client_version = DEFAULT_CLIENT_VERSION
        self.client_build_number = DEFAULT_CLIENT_BUILD_NUMBER
        self.access_token = access_token
        self.account = account_service.get_account(self.access_token) if self.access_token else {}
        self.account = self.account if isinstance(self.account, dict) else {}
        self.fp = self._build_fp()
        self.user_agent = self.fp["user-agent"]
        self.device_id = self.fp["oai-device-id"]
        self.session_id = self.fp["oai-session-id"]
        self.pow_script_sources: list[str] = []
        self.pow_data_build = ""
        self.session = requests.Session(**proxy_settings.build_session_kwargs(
            account=self.account,
            impersonate=self.fp["impersonate"],
            verify=True,
        ))
        self.session.headers.update({
            "User-Agent": self.user_agent,
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": self.fp["sec-ch-ua"],
            "Sec-Ch-Ua-Arch": '"x86"',
            "Sec-Ch-Ua-Bitness": '"64"',
            "Sec-Ch-Ua-Full-Version": '"143.0.3650.96"',
            "Sec-Ch-Ua-Full-Version-List": '"Microsoft Edge";v="143.0.3650.96", "Chromium";v="143.0.7499.147", "Not A(Brand";v="24.0.0.0"',
            "Sec-Ch-Ua-Mobile": self.fp["sec-ch-ua-mobile"],
            "Sec-Ch-Ua-Model": '""',
            "Sec-Ch-Ua-Platform": self.fp["sec-ch-ua-platform"],
            "Sec-Ch-Ua-Platform-Version": '"19.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "OAI-Device-Id": self.device_id,
            "OAI-Session-Id": self.session_id,
            "OAI-Language": "zh-CN",
            "OAI-Client-Version": self.client_version,
            "OAI-Client-Build-Number": self.client_build_number,
        })
        if self.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def _active_request_deadline(self) -> float | None:
        for attr_name in ("request_deadline", "image_request_deadline"):
            deadline = getattr(self, attr_name, None)
            if deadline is None:
                continue
            try:
                return float(deadline)
            except (TypeError, ValueError):
                continue
        return None

    @contextmanager
    def _request_deadline_scope(self, timeout_secs: float) -> Iterator[None]:
        had_deadline = hasattr(self, "request_deadline")
        previous_deadline = getattr(self, "request_deadline", None)
        try:
            next_deadline = time.monotonic() + max(0.0, float(timeout_secs))
        except (TypeError, ValueError):
            next_deadline = time.monotonic()

        previous_deadline_float: float | None = None
        if previous_deadline is not None:
            try:
                previous_deadline_float = float(previous_deadline)
            except (TypeError, ValueError):
                previous_deadline_float = None
        self.request_deadline = min(previous_deadline_float, next_deadline) if previous_deadline_float is not None else next_deadline
        try:
            yield
        finally:
            if had_deadline:
                self.request_deadline = previous_deadline
            elif hasattr(self, "request_deadline"):
                delattr(self, "request_deadline")

    def _deadline_timeout_secs(self, default_secs: float) -> float:
        deadline = self._active_request_deadline()
        if deadline is not None:
            try:
                remaining = float(deadline) - time.monotonic()
                return max(1.0, remaining)
            except (TypeError, ValueError):
                pass
        return float(default_secs)

    def _deadline_expired(self) -> bool:
        deadline = self._active_request_deadline()
        return deadline is not None and time.monotonic() >= deadline

    def _sleep_with_request_deadline(self, seconds: float) -> None:
        sleep_for = max(0.0, float(seconds))
        deadline = self._active_request_deadline()
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)

    @staticmethod
    def _is_image_file_concurrency_error(error: UpstreamHTTPError) -> bool:
        """判断文件服务是否因账号并发限制暂时拒绝请求。"""
        if error.status_code not in {429, 503}:
            return False
        body = error.body
        try:
            body_text = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
        except (TypeError, ValueError):
            body_text = repr(body)
        text = body_text.lower()
        if "upload_protection_limit_exceeded" in text:
            return False
        return "concurrency_limit" in text or "too many concurrent" in text or "throttled" in text

    def _post_image_file_request(self, path: str, **kwargs: Any) -> requests.Response:
        """请求图片文件服务，并在账号并发限流时做有界退避。"""
        for attempt in range(IMAGE_FILE_THROTTLE_RETRIES + 1):
            response = self.session.post(self.base_url + path, **kwargs)
            try:
                ensure_ok(response, path)
                return response
            except UpstreamHTTPError as exc:
                if not self._is_image_file_concurrency_error(exc) or attempt >= IMAGE_FILE_THROTTLE_RETRIES:
                    raise
                retry_delay = exc.retry_after
                if retry_delay is None:
                    retry_delay = IMAGE_FILE_THROTTLE_BACKOFF_SECS[attempt]
                logger.warning({
                    "event": "image_file_concurrency_retry",
                    "path": path,
                    "attempt": attempt + 1,
                    "wait_secs": retry_delay,
                    "status_code": exc.status_code,
                })
                self._sleep_with_request_deadline(retry_delay)
                if self._deadline_expired():
                    raise
        raise RuntimeError("unreachable image file request retry state")

    @staticmethod
    def _request_timeout_message(label: str, timeout_secs: float) -> str:
        return f"ChatGPT {label}超时（已等待 {timeout_secs:g} 秒）。"

    def _image_request_timeout_secs(self) -> float:
        return self._deadline_timeout_secs(float(config.image_poll_timeout_secs))

    def _image_stream_hard_cap_secs(self) -> float:
        deadline = self._active_request_deadline()
        if deadline is None:
            return float(config.image_poll_timeout_secs)
        try:
            return max(0.001, float(deadline) - time.monotonic())
        except (TypeError, ValueError):
            return float(config.image_poll_timeout_secs)

    def _image_timeout_message(self) -> str:
        return (
            f"ChatGPT 生图超时（已等待 {config.image_poll_timeout_secs} 秒）。"
            f"当前超时阈值可在 config.json 中调大 image_poll_timeout_secs，"
            f"也可能是账号被限流或生图队列拥堵导致。"
        )

    @staticmethod
    def _close_stream_response(response: Any) -> None:
        if getattr(response, "_stream_closed", False):
            return
        response.close()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        session = getattr(self, "session", None)
        if session:
            try:
                session.close()
            except Exception:
                pass

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def _build_fp(self) -> Dict[str, str]:
        account = self.account
        raw_fp = account.get("fp")
        fp = {str(k).lower(): str(v) for k, v in raw_fp.items()} if isinstance(raw_fp, dict) else {}
        for key in (
                "user-agent",
                "impersonate",
                "oai-device-id",
                "oai-session-id",
                "sec-ch-ua",
                "sec-ch-ua-mobile",
                "sec-ch-ua-platform",
        ):
            value = str(account.get(key) or "").strip()
            if value:
                fp[key] = value
        fp.setdefault(
            "user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
        )
        fp.setdefault("impersonate", "chrome110")
        fp.setdefault("oai-device-id", new_uuid())
        fp.setdefault("oai-session-id", new_uuid())
        fp.setdefault("sec-ch-ua", '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"')
        fp.setdefault("sec-ch-ua-mobile", "?0")
        fp.setdefault("sec-ch-ua-platform", '"Windows"')
        return fp

    def _headers(self, path: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造请求头，并补上 web 端要求的 target path/route。"""
        headers = dict(self.session.headers)
        headers["X-OpenAI-Target-Path"] = path
        headers["X-OpenAI-Target-Route"] = path
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _extract_quota_and_restore_at(limits_progress: list[Any]) -> tuple[int, str | None]:
        for item in limits_progress:
            if isinstance(item, dict) and item.get("feature_name") == "image_gen":
                return int(item.get("remaining") or 0), str(item.get("reset_after") or "") or None
        return 0, None

    def _raise_on_error(self, response: Any, path: str) -> None:
        if response.status_code == 401:
            raise InvalidAccessTokenError(f"token invalidated ({path})")
        raise RuntimeError(f"{path} failed: HTTP {response.status_code}")

    def _get_me(self) -> Dict[str, Any]:
        path = "/backend-api/me"
        response = self.session.get(self.base_url + path, headers=self._headers(path), timeout=20)
        if response.status_code != 200:
            self._raise_on_error(response, path)
        return response.json()

    def _get_conversation_init(self) -> Dict[str, Any]:
        path = "/backend-api/conversation/init"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Content-Type": "application/json"}),
            json={
                "gizmo_id": None,
                "requested_default_model": None,
                "conversation_id": None,
                "timezone_offset_min": -480,
            },
            timeout=20,
        )
        if response.status_code != 200:
            self._raise_on_error(response, path)
        return response.json()

    def _get_default_account(self) -> Dict[str, Any]:
        path = "/backend-api/accounts/check/v4-2023-04-27"
        response = self.session.get(self.base_url + path + "?timezone_offset_min=-480", headers=self._headers(path),
                                    timeout=20)
        if response.status_code != 200:
            self._raise_on_error(response, path)
        payload = response.json()
        default_account = ((payload.get("accounts") or {}).get("default") or {}).get("account") or {}
        return default_account

    def get_user_info(self) -> Dict[str, Any]:
        """获取当前 token 的账号信息。"""
        if not self.access_token:
            raise RuntimeError("access_token is required")
        executor = ThreadPoolExecutor(max_workers=3)
        try:
            me_future = executor.submit(self._get_me)
            init_future = executor.submit(self._get_conversation_init)
            account_future = executor.submit(self._get_default_account)
            me_payload, init_payload, default_account = me_future.result(), init_future.result(), account_future.result()
        except (KeyboardInterrupt, SystemExit):
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        plan_type = str(default_account.get("plan_type") or "free")

        limits_progress = init_payload.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        quota, restore_at = self._extract_quota_and_restore_at(limits_progress)
        result = {
            "email": me_payload.get("email"),
            "user_id": me_payload.get("id"),
            "type": plan_type,
            "quota": quota,
            "limits_progress": limits_progress,
            "default_model_slug": init_payload.get("default_model_slug"),
            "restore_at": restore_at,
            "status": "限流" if quota == 0 else "正常",
        }
        return result

    def _bootstrap_headers(self) -> Dict[str, str]:
        """构造首页预热请求头。"""
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Sec-Ch-Ua": self.session.headers["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": self.session.headers["Sec-Ch-Ua-Mobile"],
            "Sec-Ch-Ua-Platform": self.session.headers["Sec-Ch-Ua-Platform"],
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def _conversation_headers(self, path: str, requirements: ChatRequirements) -> Dict[str, str]:
        """根据当前 requirements 构造对话 SSE 请求头。"""
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "OpenAI-Sentinel-Chat-Requirements-Token": requirements.token,
        }
        if requirements.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
        if requirements.turnstile_token:
            headers["OpenAI-Sentinel-Turnstile-Token"] = requirements.turnstile_token
        if requirements.so_token:
            headers["OpenAI-Sentinel-SO-Token"] = requirements.so_token
        return self._headers(path, headers)

    def _api_messages_to_conversation_messages(self, messages: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """把标准 chat messages 转成 web conversation 所需的 messages。"""
        conversation_messages = []
        for item in messages:
            role = item.get("role", "user")
            content = item.get("content", "")
            if isinstance(content, str):
                conversation_messages.append({
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": [content]},
                })
                continue
            if not isinstance(content, list):
                raise RuntimeError("only string or list message content is supported")
            text_parts: list[str] = []
            image_inputs: list[tuple[bytes, str]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "")
                if part_type == "text":
                    text_parts.append(str(part.get("text") or ""))
                elif part_type == "image":
                    data = part.get("data")
                    mime = str(part.get("mime") or "image/png")
                    if isinstance(data, (bytes, bytearray)):
                        image_inputs.append((bytes(data), mime))
            if not image_inputs:
                conversation_messages.append({
                    "id": new_uuid(),
                    "author": {"role": role},
                    "content": {"content_type": "text", "parts": ["".join(text_parts)]},
                })
                continue
            if not self.access_token:
                raise RuntimeError("authenticated upstream account required for image input")
            uploaded: list[Dict[str, Any]] = []
            for idx, (data, mime) in enumerate(image_inputs, start=1):
                ext_part = mime.split("/", 1)[1].split("+")[0] if "/" in mime else "png"
                extension = "jpg" if ext_part == "jpeg" else (ext_part or "png")
                uploaded.append(self._upload_image_bytes(data, f"image_{idx}.{extension}"))
            parts: list[Any] = []
            for ref in uploaded:
                parts.append({
                    "content_type": "image_asset_pointer",
                    "asset_pointer": f"file-service://{ref['file_id']}",
                    "width": ref["width"],
                    "height": ref["height"],
                    "size_bytes": ref["file_size"],
                })
            text = "".join(text_parts)
            if text:
                parts.append(text)
            conversation_messages.append({
                "id": new_uuid(),
                "author": {"role": role},
                "content": {"content_type": "multimodal_text", "parts": parts},
                "metadata": {
                    "attachments": [{
                        "id": ref["file_id"],
                        "mimeType": ref["mime_type"],
                        "name": ref["file_name"],
                        "size": ref["file_size"],
                        "width": ref["width"],
                        "height": ref["height"],
                    } for ref in uploaded],
                },
            })
        return conversation_messages

    @staticmethod
    def _normalize_thinking_effort(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "none", "auto"}:
            return ""
        if normalized in {"low", "medium", "high", "standard", "max"}:
            return normalized
        if normalized in {"xhigh", "extended"}:
            return "extended"
        return ""

    def _conversation_payload(
            self,
            messages: list[Dict[str, Any]],
            model: str,
            timezone: str,
            thinking_effort: str = "",
    ) -> Dict[str, Any]:
        """把标准 messages 构造成 web 对话请求体。"""
        payload = {
            "action": "next",
            "messages": self._api_messages_to_conversation_messages(messages),
            "model": model,
            "parent_message_id": new_uuid(),
            "conversation_mode": {"kind": "primary_assistant"},
            "conversation_origin": None,
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "force_use_sse": True,
            "history_and_training_disabled": True,
            "reset_rate_limits": False,
            "suggestions": [],
            "supported_encodings": [],
            "system_hints": [],
            "timezone": timezone,
            "timezone_offset_min": -480,
            "websocket_request_id": new_uuid(),
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 120,
                "page_height": 900,
                "page_width": 1400,
                "pixel_ratio": 2,
                "screen_height": 1440,
                "screen_width": 2560,
            },
        }
        variant_purpose = config.upstream_variant_purpose
        if variant_purpose:
            payload["variant_purpose"] = variant_purpose
        normalized_effort = self._normalize_thinking_effort(thinking_effort or config.default_thinking_effort)
        if normalized_effort:
            payload["thinking_effort"] = normalized_effort
        return payload

    def _image_model_slug(self, model: str) -> str:
        """把标准图片模型名映射到底层 model slug。"""
        _, base_model = split_image_model(model)
        if not base_model:
            return "auto"
        if base_model == "gpt-image-2":
            return "gpt-5-5-thinking"
        if base_model == CODEX_IMAGE_MODEL:
            return base_model
        return base_model

    def _image_headers(self, path: str, requirements: ChatRequirements, conduit_token: str = "", accept: str = "*/*") -> \
            Dict[str, str]:
        """构造图片链路请求头。"""
        headers = {
            "Content-Type": "application/json",
            "Accept": accept,
            "OpenAI-Sentinel-Chat-Requirements-Token": requirements.token,
        }
        if requirements.proof_token:
            headers["OpenAI-Sentinel-Proof-Token"] = requirements.proof_token
        if conduit_token:
            headers["X-Conduit-Token"] = conduit_token
        if accept == "text/event-stream":
            headers["X-Oai-Turn-Trace-Id"] = new_uuid()
        return self._headers(path, headers)

    def _codex_responses_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _ensure_codex_source_account(self) -> None:
        account = account_service.get_account(self.access_token)
        source_type = str((account or {}).get("source_type") or "web").strip().lower()
        if source_type != "codex":
            raise RuntimeError("codex responses endpoint requires a codex source account")

    @staticmethod
    def _codex_image_input(prompt: str, images: list[str]) -> list[Dict[str, Any]]:
        content: list[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image in images:
            payload = image if image.startswith("data:image/") else f"data:image/png;base64,{image}"
            content.append({"type": "input_image", "image_url": payload})
        return [{"role": "user", "content": content}]

    def _read_codex_response_text(self, raw: Any) -> str:
        deadline = getattr(self, "image_request_deadline", None)
        if deadline is None:
            return raw.read().decode("utf-8", "replace")
        chunks: list[bytes] = []
        while True:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(self._image_timeout_message())
            chunk = raw.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def _iter_codex_response_events(self, raw: Any) -> Iterator[Dict[str, Any]]:
        content_type = str(raw.headers.get("content-type") or "").lower()
        text = self._read_codex_response_text(raw)
        events: list[Dict[str, Any]] = []
        if "application/json" in content_type:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    events.append(data)
            except Exception:
                pass
        else:
            lines: list[str] = []
            for line in text.splitlines() + [""]:
                if not line:
                    if lines:
                        payload_text = "\n".join(lines).strip()
                        if payload_text and payload_text != "[DONE]":
                            try:
                                data = json.loads(payload_text)
                            except Exception:
                                data = None
                            if isinstance(data, dict):
                                events.append(data)
                        lines = []
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())

        for event in events:
            yield event

    def iter_codex_image_response_events(
            self,
            prompt: str,
            images: list[str] | None = None,
            size: str | None = None,
            quality: str = "auto",
    ) -> Iterator[Dict[str, Any]]:
        if not self.access_token:
            raise RuntimeError("access_token is required for codex image endpoints")
        self._ensure_codex_source_account()
        path = "/backend-api/codex/responses"
        payload = {
            "model": CODEX_RESPONSES_MODEL,
            "instructions": CODEX_RESPONSES_INSTRUCTIONS,
            "store": False,
            "input": self._codex_image_input(prompt, images or []),
            "tools": [{
                "type": "image_generation",
                "model": "gpt-image-2",
                "action": "edit" if images else "generate",
                "size": str(size or "1024x1024"),
                "quality": str(quality or "auto"),
                "output_format": "png",
            }],
            "tool_choice": {"type": "image_generation"},
            "stream": True,
        }
        request = urllib.request.Request(
            self.base_url + path,
            json.dumps(payload).encode(),
            self._codex_responses_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._image_request_timeout_secs()) as raw:
                yield from self._iter_codex_response_events(raw)
        except urllib.error.HTTPError as error:
            body_text = error.read().decode("utf-8", "replace")
            body: Any = body_text
            try:
                body = json.loads(body_text)
            except Exception:
                pass
            retry_after_header = error.headers.get("Retry-After") if error.headers else None
            retry_after = int(retry_after_header) if str(retry_after_header or "").isdigit() else None
            raise UpstreamHTTPError(path, error.code, body, retry_after=retry_after) from error

    def _prepare_image_conversation(self, prompt: str, requirements: ChatRequirements, model: str) -> str:
        """为图片生成准备 conduit token。"""
        path = "/backend-api/f/conversation/prepare"
        payload = {
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": new_uuid(),
            "model": self._image_model_slug(model),
            "client_prepare_state": "success",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["picture_v2"],
            "partial_query": {
                "id": new_uuid(),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt]},
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        }
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements),
            json=payload,
            timeout=self._deadline_timeout_secs(60),
        )
        ensure_ok(response, path)
        return response.json().get("conduit_token", "")

    def _decode_image_base64(self, image: str) -> bytes:
        """把 base64 图片字符串或本地路径解码成二进制。"""
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            file_path = Path(os.path.expanduser(image))
            if file_path.exists() and file_path.is_file():
                return file_path.read_bytes()
        payload = image.split(",", 1)[1] if image.startswith("data:") and "," in image else image
        return base64.b64decode(payload)

    def _upload_image(self, image: str, file_name: str = "image.png") -> Dict[str, Any]:
        """上传一张 base64 或本地路径图片，返回底层文件元数据。"""
        data = self._decode_image_base64(image)
        if (
                image
                and len(image) < 512
                and not image.startswith("data:")
                and "\n" not in image
                and "\r" not in image
        ):
            candidate_path = Path(os.path.expanduser(image))
            if candidate_path.exists() and candidate_path.is_file():
                file_name = candidate_path.name
        return self._upload_image_bytes(data, file_name)

    def _upload_image_bytes(self, data: bytes, file_name: str = "image.png") -> Dict[str, Any]:
        """上传图片二进制，返回底层文件元数据。"""
        image = Image.open(BytesIO(data))
        width, height = image.size
        mime_type = Image.MIME.get(image.format, "image/png")
        path = "/backend-api/files"
        response = self._post_image_file_request(
            path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            json={"file_name": file_name, "file_size": len(data), "use_case": "multimodal", "width": width,
                  "height": height},
            timeout=self._deadline_timeout_secs(60),
        )
        ensure_ok(response, path)
        upload_meta = response.json()
        response = self.session.put(
            upload_meta["upload_url"],
            headers={
                "Content-Type": mime_type,
                "x-ms-blob-type": "BlockBlob",
                "x-ms-version": "2020-04-08",
                "Origin": self.base_url,
                "Referer": self.base_url + "/",
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.8",
            },
            data=data,
            timeout=self._deadline_timeout_secs(120),
        )
        ensure_ok(response, "image_upload")
        path = f"/backend-api/files/{upload_meta['file_id']}/uploaded"
        response = self._post_image_file_request(
            path,
            headers=self._headers(path, {"Content-Type": "application/json", "Accept": "application/json"}),
            data="{}",
            timeout=self._deadline_timeout_secs(60),
        )
        ensure_ok(response, path)
        return {
            "file_id": upload_meta["file_id"],
            "file_name": file_name,
            "file_size": len(data),
            "mime_type": mime_type,
            "width": width,
            "height": height,
        }

    def _start_image_generation(self, prompt: str, requirements: ChatRequirements, conduit_token: str, model: str,
                                references: Optional[list[Dict[str, Any]]] = None) -> requests.Response:
        """启动图片生成或编辑的 SSE 请求。"""
        references = references or []
        parts = [{
            "content_type": "image_asset_pointer",
            "asset_pointer": f"file-service://{item['file_id']}",
            "width": item["width"],
            "height": item["height"],
            "size_bytes": item["file_size"],
        } for item in references]
        parts.append(prompt)
        content = {"content_type": "multimodal_text", "parts": parts} if references else {"content_type": "text",
                                                                                          "parts": [prompt]}
        metadata = {
            "developer_mode_connector_ids": [],
            "selected_github_repos": [],
            "selected_all_github_repos": False,
            "system_hints": ["picture_v2"],
            "serialization_metadata": {"custom_symbol_offsets": []},
        }
        if references:
            metadata["attachments"] = [{
                "id": item["file_id"],
                "mimeType": item["mime_type"],
                "name": item["file_name"],
                "size": item["file_size"],
                "width": item["width"],
                "height": item["height"],
            } for item in references]
        payload = {
            "action": "next",
            "messages": [{
                "id": new_uuid(),
                "author": {"role": "user"},
                "create_time": time.time(),
                "content": content,
                "metadata": metadata,
            }],
            "parent_message_id": new_uuid(),
            "model": self._image_model_slug(model),
            "client_prepare_state": "sent",
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "system_hints": ["picture_v2"],
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {
                "is_dark_mode": False,
                "time_since_loaded": 1200,
                "page_height": 1072,
                "page_width": 1724,
                "pixel_ratio": 1.2,
                "screen_height": 1440,
                "screen_width": 2560,
                "app_name": "chatgpt.com",
            },
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
        }
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json=payload,
            timeout=self._image_request_timeout_secs(),
            stream=True,
        )
        ensure_ok(response, path)
        return response

    def _get_conversation(self, conversation_id: str, timeout_secs: float = 60.0) -> Dict[str, Any]:
        """获取完整 conversation 详情。"""
        path = f"/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=timeout_secs)
        ensure_ok(response, path)
        return response.json()

    def delete_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """删除本地对话记录。"""
        path = f"/backend-api/conversation/{conversation_id}"
        headers = self._headers(path, {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/c/{conversation_id}",
            "X-OpenAI-Target-Route": "/backend-api/conversation/{conversation_id}",
        })
        response = self.session.patch(
            self.base_url + path,
            headers=headers,
            json={"is_visible": False},
            timeout=60,
        )
        ensure_ok(response, path)
        return response.json()

    def search(self, prompt: str, model: str = SEARCH_MODEL, timeout_secs: float = SEARCH_TIMEOUT_SECS,
               poll_interval_secs: float = SEARCH_POLL_INTERVAL_SECS) -> Dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("access_token is required for search")
        with self._request_deadline_scope(timeout_secs):
            conduit_token = self._prepare_search_conversation(prompt, model)
            self._bootstrap()
            conversation_id = self._run_search_conversation(prompt, conduit_token, model)
            return self._wait_search_result(conversation_id, timeout_secs, poll_interval_secs)

    def _prepare_search_conversation(self, prompt: str, model: str) -> str:
        path = "/backend-api/f/conversation/prepare"
        response = self.session.post(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "*/*", "Content-Type": "application/json", "X-Conduit-Token": "no-token"}),
            json={
                "action": "next",
                "fork_from_shared_post": False,
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "system_hints": ["search"],
                "partial_query": {"id": new_uuid(), "author": {"role": "user"}, "content": {"content_type": "text", "parts": [prompt]}},
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "client_contextual_info": {"app_name": "chatgpt.com"},
            },
            timeout=self._deadline_timeout_secs(60),
        )
        ensure_ok(response, path)
        token = str(response.json().get("conduit_token") or "")
        if not token:
            raise RuntimeError("missing conduit_token")
        return token

    def _run_search_conversation(self, prompt: str, conduit_token: str, model: str) -> str:
        requirements = self._get_chat_requirements()
        path = "/backend-api/f/conversation"
        response = self.session.post(
            self.base_url + path,
            headers=self._image_headers(path, requirements, conduit_token, "text/event-stream"),
            json={
                "action": "next",
                "messages": [{
                    "id": new_uuid(),
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {"content_type": "text", "parts": [prompt]},
                    "metadata": {
                        "developer_mode_connector_ids": [],
                        "selected_github_repos": [],
                        "selected_all_github_repos": False,
                        "system_hints": ["search"],
                        "serialization_metadata": {"custom_symbol_offsets": []},
                    },
                }],
                "parent_message_id": "client-created-root",
                "model": model,
                "client_prepare_state": "success",
                "timezone_offset_min": -480,
                "timezone": "Asia/Shanghai",
                "conversation_mode": {"kind": "primary_assistant"},
                "enable_message_followups": True,
                "system_hints": [],
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "force_use_search": True,
                "client_reported_search_source": "conversation_composer_web_icon",
                "client_contextual_info": {"is_dark_mode": False, "time_since_loaded": 36, "page_height": 925, "page_width": 886, "pixel_ratio": 2, "screen_height": 1440, "screen_width": 2560, "app_name": "chatgpt.com"},
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
            },
            timeout=self._deadline_timeout_secs(300),
            stream=True,
        )
        ensure_ok(response, path)
        conversation_id = ""
        try:
            for payload in iter_sse_payloads(
                    response,
                    deadline=self._active_request_deadline(),
                    timeout_message=self._request_timeout_message("联网搜索", SEARCH_TIMEOUT_SECS),
            ):
                conversation_id = conversation_id or self._find_search_value(payload, "conversation_id")
                if payload == "[DONE]":
                    break
        finally:
            self._close_stream_response(response)
        if not conversation_id:
            raise RuntimeError("conversation_id not found in stream")
        return conversation_id

    def _wait_search_result(self, conversation_id: str, timeout_secs: float, poll_interval_secs: float) -> Dict[str, Any]:
        deadline = time.time() + timeout_secs
        last_result: Dict[str, Any] | None = None
        last_answer = ""
        stable_hits = 0
        while time.time() < deadline and not self._deadline_expired():
            try:
                last_result = self._extract_search_result(conversation_id, self._get_search_conversation(conversation_id))
            except UpstreamHTTPError as exc:
                if exc.status_code not in {404, 409, 423, 429, 500, 502, 503, 504}:
                    raise
            if last_result and last_result.get("answer"):
                if last_result.get("status") in SEARCH_DONE_STATUS:
                    return last_result
                answer = str(last_result.get("answer") or "")
                stable_hits = stable_hits + 1 if answer == last_answer else 0
                last_answer = answer
                if stable_hits >= 2:
                    return last_result
            self._sleep_with_request_deadline(poll_interval_secs)
        if last_result:
            return last_result
        raise RuntimeError(f"timed out waiting for search result: {conversation_id}")

    def _get_search_conversation(self, conversation_id: str) -> Dict[str, Any]:
        path = f"/backend-api/conversation/{conversation_id}"
        headers = self._headers(path, {"Accept": "*/*"})
        headers["Referer"] = f"{self.base_url}/c/{conversation_id}"
        headers["X-OpenAI-Target-Route"] = "/backend-api/conversation/{conversation_id}"
        response = self.session.get(self.base_url + path, headers=headers, timeout=self._deadline_timeout_secs(60))
        ensure_ok(response, path)
        return response.json()

    def _extract_search_result(self, conversation_id: str, conversation: Dict[str, Any]) -> Dict[str, Any]:
        messages = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            if ((message.get("author") or {}).get("role") or "") == "assistant":
                messages.append(message)
        message = max(messages, key=lambda item: float(item.get("create_time") or 0.0)) if messages else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        finish_details = metadata.get("finish_details") if isinstance(metadata.get("finish_details"), dict) else {}
        answer = self._search_message_text(message)
        sources = self._extract_search_sources(message)
        for url in SEARCH_URL_RE.findall(answer):
            url = self._clean_search_url(url)
            if url and all(item["url"] != url for item in sources):
                sources.append({"title": "", "url": url, "snippet": "", "source_type": ""})
        return {
            "conversation_id": conversation_id,
            "status": str(finish_details.get("type") or metadata.get("status") or self._find_search_value(message, "status") or "").strip(),
            "answer": answer,
            "sources": sources,
            "assistant_message_id": str(message.get("id") or ""),
            "create_time": float(message.get("create_time") or 0.0),
        }

    def _extract_search_sources(self, payload: Any) -> list[Dict[str, str]]:
        sources: list[Dict[str, str]] = []
        for obj in self._walk_search_dicts(payload):
            metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            url = self._clean_search_url(obj.get("url") or obj.get("link") or obj.get("source_url") or metadata.get("url"))
            if url and all(item["url"] != url for item in sources):
                sources.append({
                    "title": str(obj.get("title") or obj.get("name") or obj.get("source") or "").strip(),
                    "url": url,
                    "snippet": str(obj.get("snippet") or obj.get("text") or obj.get("description") or "").strip(),
                    "source_type": str(obj.get("type") or obj.get("source_type") or "").strip(),
                })
        return sources

    def _search_message_text(self, message: Any) -> str:
        content = message.get("content") if isinstance(message, dict) else {}
        parts = []
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                parts.append(content["text"])
            for part in content.get("parts") or []:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict):
                    parts.extend(str(part.get(key) or "") for key in ("text", "summary", "content") if part.get(key))
        elif isinstance(content, str):
            parts.append(content)
        return "\n".join(part.strip() for part in parts if str(part).strip()).strip()

    def _find_search_value(self, payload: Any, key: str) -> str:
        if isinstance(payload, str):
            match = SEARCH_CONVERSATION_ID_RE.search(payload) if key == "conversation_id" else None
            if match:
                return match.group(1)
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return ""
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            return next((found for item in payload.values() if (found := self._find_search_value(item, key))), "")
        if isinstance(payload, list):
            return next((found for item in payload if (found := self._find_search_value(item, key))), "")
        return ""

    def _walk_search_dicts(self, payload: Any) -> list[Dict[str, Any]]:
        if isinstance(payload, dict):
            return [payload, *(item for value in payload.values() for item in self._walk_search_dicts(value))]
        if isinstance(payload, list):
            return [item for value in payload for item in self._walk_search_dicts(value)]
        return []

    def _clean_search_url(self, value: Any) -> str:
        return str(value or "").strip().rstrip(".,;，。；")

    @staticmethod
    def _add_unique(values: list[str], candidates: list[str]) -> None:
        for candidate in candidates:
            if candidate and candidate not in values:
                values.append(candidate)

    @classmethod
    def _extract_image_reference_ids(cls, payload: Any) -> tuple[list[str], list[str]]:
        file_ids: list[str] = []
        sediment_ids: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, str):
                # 只提取真正的图片文件 ID（file_00000000... 格式）和 file-service:// URI
                cls._add_unique(file_ids, FILE_SERVICE_ID_RE.findall(value))
                cls._add_unique(file_ids, REAL_IMAGE_FILE_ID_RE.findall(value))
                cls._add_unique(sediment_ids, SEDIMENT_ID_RE.findall(value))
                return
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return file_ids, sediment_ids

    @classmethod
    def _has_image_asset_pointer(cls, payload: Any) -> bool:
        if isinstance(payload, dict):
            if str(payload.get("content_type") or "") == "image_asset_pointer":
                return True
            asset_pointer = str(payload.get("asset_pointer") or "")
            if asset_pointer.startswith(("file-service://", "sediment://")):
                return True
            return any(cls._has_image_asset_pointer(item) for item in payload.values())
        if isinstance(payload, list):
            return any(cls._has_image_asset_pointer(item) for item in payload)
        return False

    def _extract_image_tool_records(self, data: Dict[str, Any]) -> list[Dict[str, Any]]:
        """从 conversation 明细里提取图片工具输出记录。"""
        mapping = data.get("mapping") or {}
        records = []
        for message_id, node in mapping.items():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            metadata = message.get("metadata") or {}
            content = message.get("content") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"tool", "assistant"}:
                continue
            is_image_gen = metadata.get("async_task_type") == "image_gen"
            has_asset_pointer = self._has_image_asset_pointer(content) or self._has_image_asset_pointer(metadata)
            if role == "assistant" and not (is_image_gen or has_asset_pointer):
                continue
            file_ids, sediment_ids = self._extract_image_reference_ids({"content": content, "metadata": metadata})
            if not is_image_gen and not has_asset_pointer and not file_ids and not sediment_ids:
                continue
            records.append(
                {"message_id": message_id, "create_time": message.get("create_time") or 0, "file_ids": file_ids,
                 "sediment_ids": sediment_ids})
        return sorted(records, key=lambda item: item["create_time"])

    @staticmethod
    def _find_content_policy_error_in_conversation(data: Dict[str, Any]) -> str:
        """从对话文档中查找内容政策违规错误消息。

        上游拒绝生成图片时，错误消息会出现在 assistant 消息的文本中。
        本方法遍历所有 assistant/tool 消息，检查是否包含内容政策违规关键词，
        如果匹配则返回该消息文本（截断至 500 字符），否则返回空字符串。
        """
        mapping = data.get("mapping") or {}
        for node in mapping.values():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            role = str(author.get("role") or "").strip().lower()
            if role not in {"assistant", "tool"}:
                continue
            content = message.get("content") or {}
            # 提取消息文本
            text_parts: list[str] = []
            if isinstance(content, dict):
                msg_parts = content.get("parts") or []
                if isinstance(msg_parts, list):
                    for part in msg_parts:
                        if isinstance(part, str) and part.strip():
                            text_parts.append(part.strip())
                text_field = str(content.get("text") or "")
                if text_field.strip():
                    text_parts.append(text_field.strip())
            elif isinstance(content, str) and content.strip():
                text_parts.append(content.strip())
            msg_text = "\n".join(text_parts)
            if msg_text and _is_content_policy_error(msg_text):
                return msg_text[:500]
        return ""

    @staticmethod
    def _upstream_reply_suffix(conversation: Dict[str, Any]) -> str:
        """最后一条助手文字答复，拼在错误信息后面；还没有答复时为空。"""
        messages = [
            node["message"] for node in (conversation.get("mapping") or {}).values()
            if ((node or {}).get("message") or {}).get("author", {}).get("role") == "assistant"
        ]
        if not messages:
            return ""
        parts = (max(messages, key=lambda message: message.get("create_time") or 0).get("content") or {}).get("parts") or []
        text = "".join(part for part in parts if isinstance(part, str)).strip()
        return f"上游答复：{text}" if text else ""

    @staticmethod
    def _conversation_message_summary(conversation: Dict[str, Any]) -> list[list[Any]]:
        """超时排查用：上游对话里每条非用户消息的角色、去向、类型、状态和开头文字。"""
        rows = []
        for node in (conversation.get("mapping") or {}).values():
            message = (node or {}).get("message") or {}
            author = message.get("author") or {}
            if not message or author.get("role") in ("user", "system"):
                continue
            content = message.get("content") or {}
            text = "".join(part if isinstance(part, str) else f"<{part.get('content_type')}>"
                           for part in content.get("parts") or [])
            rows.append([
                message.get("create_time") or 0, author.get("role"), author.get("name"), message.get("recipient"),
                content.get("content_type"), message.get("status"), text[:80],
            ])
        return [row[1:] for row in sorted(rows, key=lambda row: row[0])]

    def _poll_image_results(
            self,
            conversation_id: str,
            timeout_secs: float = 120.0,
            initial_file_ids: list[str] | None = None,
            initial_sediment_ids: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Poll the conversation document until image file ids appear or budget runs out.

        - Sleeps image_poll_initial_wait_secs first (default 10s, +jitter). ChatGPT
          image generation takes ~30s; polling immediately wastes requests and trips
          a transient 429 the upstream returns within ~200ms of the SSE stream
          closing (the conversation document is not yet committed).
        - Subsequent polls are image_poll_interval_secs apart (default 10s).
        - On upstream 429 / 5xx or network errors, backs off exponentially
          (capped at 16s, +jitter) honoring Retry-After when present.
        - All sleeps stay within timeout_secs; on exhaustion raises ImagePollTimeoutError.
        """
        start = time.time()
        attempt = 0
        interval = float(config.image_poll_interval_secs)
        initial_wait = float(config.image_poll_initial_wait_secs)
        file_ids: list[str] = []
        sediment_ids: list[str] = []
        self._add_unique(file_ids, initial_file_ids or [])
        self._add_unique(sediment_ids, initial_sediment_ids or [])
        has_initial_ids = bool(file_ids or sediment_ids)
        last_hit_key: tuple[tuple[str, ...], tuple[str, ...]] | None = (
            (tuple(file_ids), tuple(sediment_ids)) if has_initial_ids else None
        )
        logger.info({
            "event": "image_poll_start",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "initial_wait_secs": initial_wait,
            "interval_secs": interval,
            "initial_file_ids": file_ids,
            "initial_sediment_ids": sediment_ids,
        })

        def _remaining() -> float:
            return timeout_secs - (time.time() - start)

        if has_initial_ids and config.image_settle_enabled:
            settle_for = min(config.image_settle_secs, max(0.0, _remaining()))
            if settle_for > 0:
                time.sleep(settle_for)
        elif initial_wait > 0:
            jitter = random.uniform(0, min(2.0, initial_wait * 0.2))
            sleep_for = min(initial_wait + jitter, max(0.0, _remaining()))
            if sleep_for > 0:
                time.sleep(sleep_for)

        def _retry_sleep(reason: str, status_code: int | None, error: str | None, retry_after: int | None) -> bool:
            # retry_after=0 means "retry immediately" — must not be coerced via falsy check.
            base = retry_after if retry_after is not None else min(2 ** min(attempt, 4), 16)
            backoff = base + random.uniform(0, 0.5)
            remaining = _remaining()
            if remaining <= 0:
                return False
            sleep_for = min(backoff, remaining)
            log_payload: Dict[str, Any] = {
                "event": "image_poll_retry",
                "conversation_id": conversation_id,
                "attempt": attempt,
                "reason": reason,
                "sleep_secs": round(sleep_for, 2),
            }
            if status_code is not None:
                log_payload["status_code"] = status_code
            if error is not None:
                log_payload["error"] = error
            logger.warning(log_payload)
            time.sleep(sleep_for)
            return True

        last_task_error = ""
        conversation: Dict[str, Any] = {}
        while _remaining() > 0:
            attempt += 1
            # 在每次轮询时，检查 /backend-api/tasks/ 是否有错误（仅记录，不中断）
            # 内容政策违规检测通过对话文本进行（在 _find_content_policy_error_in_conversation 中）
            last_task_error = ""
            try:
                tasks = self._query_backend_tasks(
                    conversation_id=conversation_id,
                    timeout_secs=min(5.0, max(0.001, _remaining())),
                )
                for task in tasks:
                    is_error, error_msg, metadata = self.check_task_error(task)
                    if is_error and error_msg:
                        last_task_error = error_msg
                        logger.info({
                            "event": "image_poll_task_error_not_blocking",
                            "conversation_id": conversation_id,
                            "attempt": attempt,
                            "error_msg": error_msg,
                            "metadata": metadata,
                        })
            except Exception as exc:
                # tasks 查询失败不影响正常轮询流程
                logger.debug({
                    "event": "image_poll_task_check_failed",
                    "conversation_id": conversation_id,
                    "attempt": attempt,
                    "error": str(exc),
                })

            try:
                conversation = self._get_conversation(
                    conversation_id,
                    timeout_secs=min(60.0, max(0.001, _remaining())),
                )
            except UpstreamHTTPError as exc:
                if exc.status_code in (429, 500, 502, 503, 504):
                    if _retry_sleep("upstream_status", exc.status_code, None, exc.retry_after):
                        continue
                    break
                raise
            except requests.exceptions.RequestException as exc:
                if _retry_sleep("network", None, str(exc), None):
                    continue
                break

            for record in self._extract_image_tool_records(conversation):
                for file_id in record["file_ids"]:
                    if file_id not in file_ids:
                        file_ids.append(file_id)
                for sediment_id in record["sediment_ids"]:
                    if sediment_id not in sediment_ids:
                        sediment_ids.append(sediment_id)

            # 检查对话文本中是否包含内容政策违规错误
            # 当上游拒绝生成图片时，错误消息会出现在对话文档的 assistant 消息中，
            # 而非 /backend-api/tasks/ 的 task error 结构中。
            # 如果在没有找到图片文件 ID 的同时检测到内容政策违规，立即中断轮询。
            if not file_ids and not sediment_ids:
                for row in self._conversation_message_summary(conversation):
                    if "图像生成请求上限" in row[-1]:
                        raise ImageQuotaExceededError(row[-1], conversation_id or "")
                    if "免费排队稍后生成" in row[-1]:
                        raise ImageQueuedError(row[-1], conversation_id or "")
                    if row[0] == "tool" and "edit or restore a specific existing image" in row[-1]:
                        raise ImageSourceMissingError(
                            "上游图像工具把这次请求当成了编辑已有图片，但没有可用的原图，未能生成图片。"
                            + self._upstream_reply_suffix(conversation),
                            conversation_id or "",
                        )
                    if row[0] == "tool" and "We experienced an error when generating images" in row[-1]:
                        raise ImageToolFailedError(
                            "上游图像工具出错，未能生成图片，请重试。" + self._upstream_reply_suffix(conversation),
                            conversation_id or "",
                        )
                policy_msg = self._find_content_policy_error_in_conversation(conversation)
                if policy_msg:
                    logger.warning({
                        "event": "image_poll_conversation_text_policy_violation",
                        "conversation_id": conversation_id,
                        "attempt": attempt,
                        "error_msg": policy_msg[:200],
                    })
                    raise ImageContentPolicyError(policy_msg, conversation_id or "")

            logger.debug({"event": "image_poll_check", "conversation_id": conversation_id, "attempt": attempt,
                          "file_ids": file_ids, "sediment_ids": sediment_ids})
            if file_ids or sediment_ids:
                if not config.image_check_before_hit_enabled:
                    # 先check再hit 机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_no_settle", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                hit_key = (tuple(file_ids), tuple(sediment_ids))
                if last_hit_key == hit_key:
                    logger.info({"event": "image_poll_hit", "conversation_id": conversation_id, "file_ids": file_ids,
                                 "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                last_hit_key = hit_key
                if not config.image_settle_enabled:
                    # 二次确认机制关闭：直接返回首次发现的 file_ids
                    logger.info({"event": "image_poll_hit_settle_disabled", "conversation_id": conversation_id,
                                 "file_ids": file_ids, "sediment_ids": sediment_ids})
                    return file_ids, sediment_ids
                logger.info({"event": "image_poll_hit_pending_settle", "conversation_id": conversation_id,
                             "file_ids": file_ids, "sediment_ids": sediment_ids,
                             "settle_secs": config.image_settle_secs})
                wait = min(config.image_settle_secs, max(0.0, _remaining()))
                if wait > 0:
                    time.sleep(wait)
                    continue
                return file_ids, sediment_ids
            logger.debug({"event": "image_poll_wait", "conversation_id": conversation_id,
                          "elapsed_secs": round(time.time() - start, 1)})
            wait = min(interval, max(0.0, _remaining()))
            if wait > 0:
                time.sleep(wait)
        logger.info({
            "event": "image_poll_timeout",
            "conversation_id": conversation_id,
            "timeout_secs": timeout_secs,
            "attempts_made": attempt,
            # attempts_made == 0 means the initial_wait consumed the entire budget — no HTTP attempted.
            "initial_wait_exhausted_budget": attempt == 0,
            "last_task_error": last_task_error if last_task_error else None,
            "upstream_messages": self._conversation_message_summary(conversation),
        })
        exc = ImagePollTimeoutError(
            f"ChatGPT 生图超时（已等待 {timeout_secs} 秒）。"
            f"当前超时阈值可在 config.json 中调大 image_poll_timeout_secs，"
            f"也可能是账号被限流或生图队列拥堵导致。",
            conversation_id or "",
        )
        if last_task_error:
            setattr(exc, "task_error", last_task_error)
        raise exc

    def _get_file_download_url(self, file_id: str, timeout_secs: float = 60.0) -> str:
        """获取文件下载地址。"""
        path = f"/backend-api/files/{file_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=timeout_secs)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _get_attachment_download_url(
        self,
        conversation_id: str,
        attachment_id: str,
        timeout_secs: float = 60.0,
    ) -> str:
        """通过 conversation 附件接口获取下载地址。"""
        path = f"/backend-api/conversation/{conversation_id}/attachment/{attachment_id}/download"
        response = self.session.get(self.base_url + path, headers=self._headers(path, {"Accept": "application/json"}),
                                    timeout=timeout_secs)
        ensure_ok(response, path)
        data = response.json()
        return data.get("download_url") or data.get("url") or ""

    def _query_backend_tasks(
        self,
        conversation_id: str = "",
        task_id: str = "",
        timeout_secs: float = 30.0,
    ) -> list[Dict[str, Any]]:
        """查询 /backend-api/tasks/ 接口获取异步任务状态和错误信息。

        参数：
        - `conversation_id`：可选。按 conversation_id 过滤任务。
        - `task_id`：可选。按 task_id 过滤任务。
        - `timeout_secs`：请求超时秒数。

        返回：
        - 任务列表，每个任务包含 image_gen_message 等字段。
        """
        path = "/backend-api/tasks"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(path, {"Accept": "application/json"}),
            timeout=timeout_secs,
        )
        ensure_ok(response, path)
        data = response.json()
        tasks = data.get("tasks", [])
        if not isinstance(tasks, list):
            return []

        # 按 conversation_id 或 task_id 过滤
        if conversation_id:
            tasks = [
                t for t in tasks
                if isinstance(t, dict) and (
                    t.get("conversation_id") == conversation_id
                    or t.get("original_conversation_id") == conversation_id
                )
            ]
        if task_id:
            tasks = [t for t in tasks if isinstance(t, dict) and t.get("task_id") == task_id]
        return tasks

    def check_task_error(self, task: Dict[str, Any]) -> tuple[bool, str, Dict[str, Any]]:
        """检查单个任务是否包含结构化错误。

        通过以下字段判断（不依赖文本匹配）：
        - image_gen_message.metadata.is_error == True
        - image_gen_message.author.role == "assistant" (而非 "tool")
        - image_gen_message.content.content_type == "text" (而非 "multimodal_text")

        返回：
        - (is_error, error_msg, metadata)
        """
        img_msg = task.get("image_gen_message") or {}
        if not img_msg:
            return False, "", {}

        metadata = img_msg.get("metadata") or {}
        content = img_msg.get("content") or {}
        author = img_msg.get("author") or {}

        is_error = metadata.get("is_error", False)
        is_text_only = content.get("content_type") == "text"
        is_assistant_role = author.get("role") == "assistant"

        # 提取错误文本
        error_msg = ""
        if is_error and is_text_only:
            parts = content.get("parts", [])
            error_msg = "".join(p for p in parts if isinstance(p, str))

        return is_error, error_msg, metadata

    def _resolve_image_urls(self, conversation_id: str, file_ids: list[str], sediment_ids: list[str]) -> list[str]:
        """把图片结果 id 解析成可下载 URL。"""
        urls = []
        skip_patterns = {"file_upload"}
        for file_id in file_ids:
            if file_id in skip_patterns:
                logger.debug({
                    "event": "image_file_id_skipped",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
                continue
            try:
                url = self._get_file_download_url(file_id, timeout_secs=self._deadline_timeout_secs(60))
            except Exception as exc:
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                    "error": repr(exc),
                })
                continue
            if url:
                if url not in urls:
                    urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "file",
                    "conversation_id": conversation_id,
                    "id": file_id,
                })
        if not conversation_id or not sediment_ids:
            logger.debug({
                "event": "image_urls_resolved",
                "conversation_id": conversation_id,
                "file_ids": file_ids,
                "sediment_ids": sediment_ids,
                "urls": urls,
            })
            return urls
        for sediment_id in sediment_ids:
            try:
                url = self._get_attachment_download_url(
                    conversation_id,
                    sediment_id,
                    timeout_secs=self._deadline_timeout_secs(60),
                )
            except Exception as exc:
                logger.debug({
                    "event": "image_download_url_failed",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                    "error": repr(exc),
                })
                continue
            if url:
                if url not in urls:
                    urls.append(url)
            else:
                logger.debug({
                    "event": "image_download_url_empty",
                    "source": "sediment",
                    "conversation_id": conversation_id,
                    "id": sediment_id,
                })
        logger.debug({
            "event": "image_urls_resolved",
            "conversation_id": conversation_id,
            "file_ids": file_ids,
            "sediment_ids": sediment_ids,
            "urls": urls,
        })
        return urls

    def resolve_conversation_image_urls(
            self,
            conversation_id: str,
            file_ids: list[str],
            sediment_ids: list[str],
            poll: bool = True,
            poll_timeout_secs: float | None = None,
    ) -> list[str]:
        file_ids = [item for item in file_ids if item != "file_upload"]
        sediment_ids = list(sediment_ids)
        timeout = poll_timeout_secs if poll_timeout_secs is not None else config.image_poll_timeout_secs
        # 当 check-before-hit 和 settle 均已关闭，且 SSE 已给出 file_ids 时，
        # 跳过轮询直接解析 URL，省去 initial_wait + 轮询耗时。
        if poll and conversation_id and (file_ids or sediment_ids):
            if not config.image_check_before_hit_enabled and not config.image_settle_enabled:
                logger.info({
                    "event": "image_resolve_skip_poll_direct_resolve",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
                return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)
        if poll and conversation_id:
            logger.info({
                "event": "image_resolve_poll_needed",
                "conversation_id": conversation_id,
                "initial_file_ids": file_ids,
                "initial_sediment_ids": sediment_ids,
                "poll_timeout_secs": timeout,
            })
            try:
                polled_file_ids, polled_sediment_ids = self._poll_image_results(
                    conversation_id,
                    timeout,
                    file_ids,
                    sediment_ids,
                )
            except ImagePollTimeoutError as exc:
                # 如果轮询超时且有 task error（如 moderation 拦截），抛出 ImageContentPolicyError
                # 而非 ImagePollTimeoutError，让调用方能区分真正的超时和上游拒绝
                task_error = getattr(exc, "task_error", "")
                if not file_ids and not sediment_ids:
                    if task_error:
                        raise ImageContentPolicyError(task_error, conversation_id or "") from exc
                    raise
                logger.warning({
                    "event": "image_resolve_poll_partial_timeout",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                })
            except Exception as exc:
                if not file_ids and not sediment_ids:
                    raise
                logger.warning({
                    "event": "image_resolve_poll_partial_error",
                    "conversation_id": conversation_id,
                    "file_ids": file_ids,
                    "sediment_ids": sediment_ids,
                    "error": repr(exc),
                })
            else:
                file_ids.extend(item for item in polled_file_ids if item and item not in file_ids)
                sediment_ids.extend(item for item in polled_sediment_ids if item and item not in sediment_ids)
        return self._resolve_image_urls(conversation_id, file_ids, sediment_ids)

    def download_image_bytes(self, urls: list[str]) -> list[bytes]:
        images = []
        for url in urls:
            response = self.session.get(url, timeout=self._deadline_timeout_secs(120))
            ensure_ok(response, "image_download")
            if response.content not in images:
                images.append(response.content)
        return images

    def stream_conversation(
            self,
            messages: Optional[list[Dict[str, Any]]] = None,
            model: str = "auto",
            prompt: str = "",
            images: Optional[list[str]] = None,
            system_hints: Optional[list[str]] = None,
            thinking_effort: str = "",
    ) -> Iterator[str]:
        system_hints = system_hints or []
        if "picture_v2" in system_hints:
            yield from self._stream_picture_conversation(prompt, model, images or [])
            return

        with self._request_deadline_scope(TEXT_STREAM_TIMEOUT_SECS):
            normalized = messages or [{"role": "user", "content": prompt}]
            self._bootstrap()
            requirements = self._get_chat_requirements()
            path, timezone = self._chat_target()
            payload = self._conversation_payload(normalized, model, timezone, thinking_effort=thinking_effort)
            response = self.session.post(
                self.base_url + path,
                headers=self._conversation_headers(path, requirements),
                json=payload,
                timeout=self._deadline_timeout_secs(TEXT_STREAM_TIMEOUT_SECS),
                stream=True,
            )
            ensure_ok(response, path)
            try:
                yield from iter_sse_payloads(
                    response,
                    deadline=self._active_request_deadline(),
                    timeout_message=self._request_timeout_message("文本生成", TEXT_STREAM_TIMEOUT_SECS),
                )
            finally:
                self._close_stream_response(response)

    def _stream_picture_conversation(
            self,
            prompt: str,
            model: str,
            images: list[str],
    ) -> Iterator[str]:
        if not self.access_token:
            raise RuntimeError("access_token is required for image endpoints")
        references = [self._upload_image(image, f"image_{idx}.png") for idx, image in enumerate(images, start=1)]
        self._bootstrap()
        requirements = self._get_chat_requirements()
        conduit_token = self._prepare_image_conversation(prompt, requirements, model)
        response = self._start_image_generation(prompt, requirements, conduit_token, model, references)
        yield from self._iter_sse_payloads_capped(response, self._image_stream_hard_cap_secs())

    def _iter_sse_payloads_capped(self, response: Any, hard_cap_secs: float) -> Iterator[str]:
        """按墙钟硬上限消费图片 SSE 流，超时后关闭连接，避免上游长连接一直挂起。"""
        try:
            normalized_cap = max(0.001, float(hard_cap_secs))
        except (TypeError, ValueError):
            normalized_cap = float(config.image_poll_timeout_secs)
        deadline = time.monotonic() + normalized_cap
        forced_close = threading.Event()

        def close_blocked_stream() -> None:
            forced_close.set()
            try:
                response.close()
            except Exception:
                pass

        watchdog = threading.Timer(normalized_cap, close_blocked_stream)
        watchdog.daemon = True
        watchdog.start()
        timeout_message = (
            f"图片生成流已超过硬上限 {normalized_cap:g} 秒，"
            "已强制中断（上游可能未生成图片）"
        )
        try:
            for payload in iter_sse_payloads(
                response,
                deadline=deadline,
                timeout_message=timeout_message,
            ):
                if forced_close.is_set() or time.monotonic() >= deadline:
                    raise ImageStreamHardTimeoutError(timeout_message)
                yield payload
            if forced_close.is_set() or time.monotonic() >= deadline:
                raise ImageStreamHardTimeoutError(timeout_message)
        except ImageStreamHardTimeoutError:
            raise
        except TimeoutError as exc:
            raise ImageStreamHardTimeoutError(timeout_message) from exc
        except Exception as exc:
            if forced_close.is_set() or time.monotonic() >= deadline:
                raise ImageStreamHardTimeoutError(timeout_message) from exc
            raise
        finally:
            watchdog.cancel()
            if not forced_close.is_set():
                try:
                    self._close_stream_response(response)
                except Exception:
                    pass

    def _bootstrap(self) -> None:
        """预热首页，并提取 PoW 相关脚本引用。"""
        response = self.session.get(
            self.base_url + "/",
            headers=self._bootstrap_headers(),
            timeout=self._deadline_timeout_secs(30),
        )
        ensure_ok(response, "bootstrap")
        self.pow_script_sources, self.pow_data_build = parse_pow_resources(response.text)
        if not self.pow_script_sources:
            self.pow_script_sources = [DEFAULT_POW_SCRIPT]

    def _get_chat_requirements(self) -> ChatRequirements:
        """获取当前模式对话所需的 sentinel token（prepare + finalize 两步流程）。"""
        base = "/backend-api/sentinel/chat-requirements" if self.access_token else "/backend-anon/sentinel/chat-requirements"
        p_token = build_legacy_requirements_token(self.user_agent, self.pow_script_sources, self.pow_data_build)

        prepare_path = base + "/prepare"
        response = self.session.post(
            self.base_url + prepare_path,
            headers=self._headers(prepare_path, {"Content-Type": "application/json"}),
            json={"p": p_token},
            timeout=self._deadline_timeout_secs(30),
        )
        ensure_ok(response, "chat_requirements_prepare")
        prepare_data = response.json()

        if (prepare_data.get("arkose") or {}).get("required"):
            raise RuntimeError("chat requirements requires arkose token, which is not implemented")

        proof_token = ""
        proof_info = prepare_data.get("proofofwork") or {}
        if proof_info.get("required"):
            proof_token = build_proof_token(
                proof_info.get("seed", ""),
                proof_info.get("difficulty", ""),
                self.user_agent,
                script_sources=self.pow_script_sources,
                data_build=self.pow_data_build,
            )

        turnstile_token = ""
        turnstile_info = prepare_data.get("turnstile") or {}
        if turnstile_info.get("required") and turnstile_info.get("dx"):
            turnstile_token = solve_turnstile_token(turnstile_info["dx"], p_token) or ""

        finalize_path = base + "/finalize"
        response = self.session.post(
            self.base_url + finalize_path,
            headers=self._headers(finalize_path, {"Content-Type": "application/json"}),
            json={
                "prepare_token": prepare_data.get("prepare_token", ""),
                "proof_token": proof_token,
                "turnstile_token": turnstile_token,
            },
            timeout=self._deadline_timeout_secs(30),
        )
        ensure_ok(response, "chat_requirements_finalize")
        data = response.json()

        token = data.get("token", "")
        if not token:
            message = "missing auth chat requirements token" if self.access_token else "missing chat requirements token"
            raise RuntimeError(f"{message}: {data}")

        return ChatRequirements(
            token=token,
            proof_token=proof_token,
            turnstile_token=turnstile_token,
            so_token=data.get("so_token", ""),
            raw_finalize=data,
        )

    def _chat_target(self) -> tuple[str, str]:
        if self.access_token:
            return "/backend-api/conversation", "Asia/Shanghai"
        return "/backend-anon/conversation", "America/Los_Angeles"

    def list_models(self) -> Dict[str, Any]:
        """返回当前模式下可用模型，格式对齐 OpenAI `/v1/models`。"""
        self._bootstrap()
        path = "/backend-api/models?history_and_training_disabled=false" if self.access_token else (
            "/backend-anon/models?iim=false&is_gizmo=false"
        )
        route = "/backend-api/models" if self.access_token else "/backend-anon/models"
        context = "auth_models" if self.access_token else "anon_models"
        response = self.session.get(
            self.base_url + path,
            headers=self._headers(route),
            timeout=30,
        )
        ensure_ok(response, context)
        data = []
        seen = set()
        for item in response.json().get("models", []):
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug", "")).strip()
            if not slug or slug in seen:
                continue
            seen.add(slug)
            data.append({
                "id": slug,
                "object": "model",
                "created": int(item.get("created") or 0),
                "owned_by": str(item.get("owned_by") or "chatgpt"),
                "permission": [],
                "root": slug,
                "parent": None,
            })
        data.sort(key=lambda item: item["id"])
        return {"object": "list", "data": data}
