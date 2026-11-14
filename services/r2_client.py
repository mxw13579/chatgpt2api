"""Small R2 S3 client shared by backups and image storage (no SDK or retries)."""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlencode
from xml.etree import ElementTree

from curl_cffi import requests


class R2Error(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _clean(value: object) -> str:
    return str(value or "").strip()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class CloudflareR2Client:
    # Keep the original backup constructor: a mapping containing these five fields.
    def __init__(self, settings: dict[str, object]) -> None:
        self.account_id = _clean(settings.get("account_id"))
        self.access_key_id = _clean(settings.get("access_key_id"))
        self.secret_access_key = _clean(settings.get("secret_access_key"))
        self.bucket = _clean(settings.get("bucket"))
        self.prefix = _clean(settings.get("prefix")) or "backups"
        self.session = requests.Session(verify=True, trust_env=False)

    def validate(self) -> None:
        missing = []
        for label, value in (
            ("Account ID", self.account_id),
            ("Access Key ID", self.access_key_id),
            ("Secret Access Key", self.secret_access_key),
            ("Bucket", self.bucket),
        ):
            if not value:
                missing.append(label)
        if missing:
            raise R2Error(f"R2 配置不完整：缺少 {'、'.join(missing)}")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.account_id):
            raise R2Error("R2 Account ID 格式无效")

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def _aws_v4_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        now = _utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        # SigV4 requires percent-encoded spaces, not form encoding's '+'.
        encoded_query = urlencode(sorted((query or {}).items()), quote_via=quote, safe="~")
        payload_hash = _sha256_hex(body)
        headers = {
            "host": f"{self.account_id}.r2.cloudflarestorage.com",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            for key, value in extra_headers.items():
                headers[key.lower()] = " ".join(value.strip().split())
        sorted_items = sorted(headers.items())
        canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted_items)
        signed_headers = ";".join(key for key, _ in sorted_items)
        canonical_request = "\n".join([
            method.upper(), path, encoded_query, canonical_headers, signed_headers, payload_hash,
        ])
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        k_date = hmac.new(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
        k_region = hmac.new(k_date, b"auto", hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return encoded_query, headers

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ):
        self.validate()
        object_path = f"/{quote(self.bucket, safe='')}"
        if key:
            object_path += f"/{quote(key.lstrip('/'), safe='/~')}"
        encoded_query, headers = self._aws_v4_headers(method, object_path, query=query, body=body, extra_headers=extra_headers)
        url = f"{self.endpoint}{object_path}"
        if encoded_query:
            url += f"?{encoded_query}"
        try:
            return self.session.request(
                method.upper(), url, headers=headers, data=body, timeout=timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException:
            # curl exceptions may contain the signed URL or request credentials.
            raise R2Error("R2 网络请求失败") from None

    @staticmethod
    def _check_response(response, operation: str) -> None:
        if not 200 <= response.status_code < 300:
            raise R2Error(f"{operation}：HTTP {response.status_code}", status_code=int(response.status_code))

    def test_connection(self) -> dict[str, object]:
        # ListObjects is Class A; callers must reserve an operation before calling.
        response = self._request("GET", query={"list-type": "2", "max-keys": "1"}, timeout=30.0)
        self._check_response(response, "连接 R2 失败")
        return {"ok": True, "status": int(response.status_code)}

    def upload_bytes(
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
        cache_control: str | None = None,
    ) -> dict[str, object]:
        headers = {"content-type": content_type}
        if metadata:
            for item_key, item_value in metadata.items():
                headers[f"x-amz-meta-{item_key}"] = str(item_value)
        if cache_control is not None:
            headers["cache-control"] = cache_control
        response = self._request("PUT", key, body=payload, extra_headers=headers)
        self._check_response(response, "上传 R2 对象失败")
        return {"key": key, "etag": str(response.headers.get("etag") or "").strip('"')}

    def delete_object(self, key: str) -> None:
        response = self._request("DELETE", key, timeout=30.0)
        if response.status_code != 404:
            self._check_response(response, "删除 R2 对象失败")

    def download_bytes(self, key: str) -> bytes:
        response = self._request("GET", key, timeout=60.0)
        self._check_response(response, "读取 R2 对象失败")
        return bytes(response.content or b"")

    def get(self, key: str) -> bytes:
        return self.download_bytes(key)

    def list_objects(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        continuation = ""
        seen_tokens: set[str] = set()
        while True:
            query = {"list-type": "2", "prefix": f"{self.prefix.rstrip('/')}/", "max-keys": "1000"}
            if continuation:
                query["continuation-token"] = continuation
            response = self._request("GET", query=query, timeout=30.0)
            self._check_response(response, "获取 R2 对象列表失败")
            try:
                root = ElementTree.fromstring(response.content)
                # S3 responses normally use a default namespace.
                for block in root.findall("{*}Contents"):
                    key = block.findtext("{*}Key", default="")
                    if not key:
                        continue
                    items.append({
                        "key": key,
                        "size": int(block.findtext("{*}Size", default="0")),
                        "updated_at": block.findtext("{*}LastModified", default=""),
                    })
            except (ElementTree.ParseError, ValueError):
                raise R2Error("R2 对象列表响应无效") from None
            if root.findtext("{*}IsTruncated", default="false") != "true":
                break
            continuation = root.findtext("{*}NextContinuationToken", default="")
            if not continuation or continuation in seen_tokens:
                raise R2Error("R2 对象列表分页不完整")
            seen_tokens.add(continuation)
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def close(self) -> None:
        self.session.close()
