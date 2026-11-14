"""Cloudflare REST/GraphQL operations needed for R2 image storage.

No cloud calls at import, retries, redirects, or implicit environment proxy settings.
Analytics are conservative request estimates, not an invoice/free-tier guarantee.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from curl_cffi import requests


_BASE_URL = "https://api.cloudflare.com/client/v4"
_USAGE_LIMIT = 10_000
_MAX_USAGE_RANGE = timedelta(days=31)

# R2 Standard pricing classes: https://developers.cloudflare.com/r2/pricing/
_CLASS_A = frozenset({
    "ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject",
    "CompleteMultipartUpload", "CreateMultipartUpload", "ListMultipartUploads",
    "UploadPart", "UploadPartCopy", "ListParts", "PutBucketEncryption",
    "PutBucketLifecycleConfiguration", "PutBucketCors",
})
_CLASS_B = frozenset({
    "HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption",
    "GetBucketLocation", "GetBucketLifecycleConfiguration", "GetBucketCors",
})
_FREE_ACTIONS = frozenset({"DeleteObject", "DeleteBucket", "AbortMultipartUpload"})
_KNOWN_STATUSES = frozenset({"success", "userError", "internalError"})

_USAGE_QUERY = """
query R2ImageUsage($accountTag: string!, $startDate: Time!, $endDate: Time!, $storageStart: Time!) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      r2OperationsAdaptiveGroups(
        limit: 10000,
        filter: {datetime_geq: $startDate, datetime_leq: $endDate}
      ) {
        sum { requests }
        dimensions { actionType actionStatus bucketName }
      }
      r2StorageAdaptiveGroups(
        limit: 10000,
        filter: {datetime_geq: $storageStart, datetime_leq: $endDate},
        orderBy: [datetime_DESC]
      ) {
        max { objectCount payloadSize metadataSize }
        dimensions { datetime bucketName }
      }
    }
  }
}
"""


class CloudflareAPIError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, codes: tuple[int, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.codes = codes


def _api_error(status: int, payload: object) -> CloudflareAPIError:
    codes = []
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, list):
        for error in errors:
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            extensions = error.get("extensions")
            if code is None and isinstance(extensions, dict):
                code = extensions.get("code")
            if type(code) is int:
                codes.append(code)
            elif isinstance(code, str) and re.fullmatch(r"[0-9]{1,10}", code):
                codes.append(int(code))
    detail = f"HTTP {status}"
    if codes:
        detail += "；错误码 " + ", ".join(str(code) for code in codes)
    # Never include CF messages, response bodies, headers or request URLs.
    return CloudflareAPIError(f"Cloudflare API 请求失败（{detail}）", status_code=status, codes=tuple(codes))


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise CloudflareAPIError("R2 用量时间必须是 UTC ISO 时间") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CloudflareAPIError("R2 用量时间必须是 UTC ISO 时间")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _count(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0 and value.is_integer():
        return int(value)
    raise CloudflareAPIError("Cloudflare R2 用量计数缺失或无效")


class CloudflareAPI:
    def __init__(self, account_id: str, api_token: str) -> None:
        self.account_id = account_id.strip()
        self.api_token = api_token.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.account_id) or not self.api_token:
            raise CloudflareAPIError("Cloudflare Account ID 或 API Token 配置不完整或无效")
        self.session = requests.Session(verify=True, trust_env=False)

    def _send(self, method: str, path: str, *, json=None, params=None) -> dict:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise CloudflareAPIError("Cloudflare API 路径无效")
        try:
            response = self.session.request(
                method.upper(), _BASE_URL + path,
                headers={"Authorization": f"Bearer {self.api_token}", "Accept": "application/json"},
                json=json, params=params, timeout=30.0, allow_redirects=False,
            )
        except requests.exceptions.RequestException:
            raise CloudflareAPIError("Cloudflare API 网络请求失败") from None
        try:
            payload = response.json()
        except ValueError:
            raise _api_error(int(response.status_code), None) from None
        if not 200 <= response.status_code < 300:
            raise _api_error(int(response.status_code), payload)
        if not isinstance(payload, dict):
            raise CloudflareAPIError("Cloudflare API 响应格式无效")
        if payload.get("errors") or payload.get("success") is False:
            raise _api_error(int(response.status_code), payload)
        return payload

    def _rest(self, method: str, path: str, *, json=None, params=None) -> dict:
        payload = self._send(method, path, json=json, params=params)
        if payload.get("success") is not True or "result" not in payload:
            raise CloudflareAPIError("Cloudflare API 响应未确认成功")
        return payload

    def request(self, method: str, path: str, *, json=None, params=None):
        return self._rest(method, path, json=json, params=params)["result"]

    def graphql(self, query: str, variables: dict) -> dict:
        payload = self._send("POST", "/graphql", json={"query": query, "variables": variables})
        if not isinstance(payload.get("data"), dict):
            raise CloudflareAPIError("Cloudflare GraphQL 数据缺失")
        return payload["data"]

    def list_zones(self) -> list[dict]:
        items = []
        page = 1
        total_pages = None
        seen_ids: set[str] = set()
        while True:
            payload = self._rest("GET", "/zones", params={
                "account.id": self.account_id, "page": page, "per_page": 50,
            })
            result = payload["result"]
            info = payload.get("result_info")
            if not isinstance(result, list) or not isinstance(info, dict):
                raise CloudflareAPIError("Cloudflare Zone 分页数据缺失")
            pages = info.get("total_pages")
            if type(pages) is not int or not 0 <= pages <= 1000 or info.get("page") != page:
                raise CloudflareAPIError("Cloudflare Zone 分页数据无效")
            if total_pages is not None and pages != total_pages:
                raise CloudflareAPIError("Cloudflare Zone 分页数据不完整")
            total_pages = pages
            if len(result) > 50 or (not result and page < total_pages) or (pages == 0 and result):
                raise CloudflareAPIError("Cloudflare Zone 分页数据不完整")
            for item in result:
                if not isinstance(item, dict) or not isinstance(item.get("account"), dict):
                    raise CloudflareAPIError("Cloudflare Zone 账号归属缺失")
                if item["account"].get("id") != self.account_id:
                    continue
                zone_id = item.get("id")
                if not isinstance(zone_id, str) or not zone_id or zone_id in seen_ids:
                    raise CloudflareAPIError("Cloudflare Zone 分页数据不完整")
                seen_ids.add(zone_id)
                items.append(item)
            if page >= total_pages:
                return items
            page += 1

    def _bucket_path(self, bucket: str) -> str:
        return f"/accounts/{self.account_id}/r2/buckets/{quote(bucket, safe='')}"

    def list_domains(self, bucket: str) -> list[dict]:
        result = self.request("GET", self._bucket_path(bucket) + "/domains/custom")
        if not isinstance(result, dict) or not isinstance(result.get("domains"), list):
            raise CloudflareAPIError("Cloudflare R2 域名列表响应无效")
        return result["domains"]

    def add_domain(self, bucket: str, domain: str, zone_id: str, enabled: bool = False) -> dict:
        return self.request("POST", self._bucket_path(bucket) + "/domains/custom", json={
            "domain": domain, "zoneId": zone_id, "enabled": enabled,
        })

    def update_domain(self, bucket: str, domain: str, enabled: bool) -> dict:
        return self.request("PUT", self._bucket_path(bucket) + "/domains/custom/" + quote(domain, safe=""), json={
            "enabled": enabled,
        })

    def delete_domain(self, bucket: str, domain: str):
        return self.request("DELETE", self._bucket_path(bucket) + "/domains/custom/" + quote(domain, safe=""))

    def get_managed_domain(self, bucket: str) -> dict:
        return self.request("GET", self._bucket_path(bucket) + "/domains/managed")

    def set_managed_domain(self, bucket: str, enabled: bool) -> dict:
        return self.request("PUT", self._bucket_path(bucket) + "/domains/managed", json={"enabled": enabled})

    def set_cors(self, bucket: str, origins: list[str]):
        return self.request("PUT", self._bucket_path(bucket) + "/cors", json={"rules": [{
            "allowed": {
                "origins": origins, "methods": ["GET", "HEAD"], "headers": ["range", "content-type"],
            },
            "exposeHeaders": ["ETag", "Content-Length"],
        }]})

    def query_usage(self, start: str, end: str, bucket: str) -> dict:
        start_date = _utc_datetime(start)
        end_date = _utc_datetime(end)
        if end_date <= start_date or end_date - start_date > _MAX_USAGE_RANGE:
            raise CloudflareAPIError("R2 用量查询区间必须大于 0 且不超过 31 天")
        data = self.graphql(_USAGE_QUERY, {
            "accountTag": self.account_id, "startDate": _iso(start_date), "endDate": _iso(end_date),
            "storageStart": _iso(end_date - timedelta(hours=24)),
        })
        viewer = data.get("viewer")
        accounts = viewer.get("accounts") if isinstance(viewer, dict) else None
        if not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(accounts[0], dict):
            raise CloudflareAPIError("Cloudflare R2 用量账号数据缺失")
        operations = accounts[0].get("r2OperationsAdaptiveGroups")
        storage = accounts[0].get("r2StorageAdaptiveGroups")
        if not isinstance(operations, list) or not isinstance(storage, list):
            raise CloudflareAPIError("Cloudflare R2 用量数据缺失")
        # These group queries have no cursor. Hitting the cap cannot prove coverage.
        complete = len(operations) < _USAGE_LIMIT and len(storage) < _USAGE_LIMIT
        result = {
            "a_operations": 0, "b_operations": 0,
            "bucket_a_operations": 0, "bucket_b_operations": 0,
            "storage_bytes": 0, "bucket_storage_bytes": 0, "object_count": 0,
            "period_start": _iso(start_date), "period_end": _iso(end_date),
        }
        unknown_actions: set[str] = set()
        for row in operations:
            if not isinstance(row, dict) or not isinstance(row.get("dimensions"), dict) or not isinstance(row.get("sum"), dict):
                raise CloudflareAPIError("Cloudflare R2 操作用量响应无效")
            dims = row["dimensions"]
            action, status, name = dims.get("actionType"), dims.get("actionStatus"), dims.get("bucketName")
            count = _count(row["sum"].get("requests"))
            if not isinstance(name, str):
                raise CloudflareAPIError("Cloudflare R2 操作用量桶归属缺失")
            if not isinstance(action, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", action):
                unknown_actions.add("<invalid>")
                complete = False
                continue
            if not isinstance(status, str) or status not in _KNOWN_STATUSES:
                complete = False
            if action in _CLASS_A:
                operation_class = "a"
            elif action in _CLASS_B:
                operation_class = "b"
            elif action in _FREE_ACTIONS:
                continue
            else:
                unknown_actions.add(action)
                complete = False
                continue
            # Internal failures are not billable. User errors remain conservatively
            # counted: this dimension cannot distinguish every exempt HTTP status.
            if status == "internalError":
                continue
            result[f"{operation_class}_operations"] += count
            if name == bucket:
                result[f"bucket_{operation_class}_operations"] += count

        latest: dict[str, tuple[datetime, int, int]] = {}
        storage_start = end_date - timedelta(hours=24)
        for row in storage:
            if not isinstance(row, dict) or not isinstance(row.get("dimensions"), dict) or not isinstance(row.get("max"), dict):
                raise CloudflareAPIError("Cloudflare R2 存储用量响应无效")
            dims, maximum = row["dimensions"], row["max"]
            name = dims.get("bucketName")
            if not isinstance(name, str) or not name:
                raise CloudflareAPIError("Cloudflare R2 存储用量桶归属缺失")
            timestamp = _utc_datetime(dims.get("datetime"))
            size = _count(maximum.get("payloadSize")) + _count(maximum.get("metadataSize"))
            objects = _count(maximum.get("objectCount"))
            if not storage_start <= timestamp <= end_date:
                complete = False
                continue
            previous = latest.get(name)
            if previous is None or timestamp > previous[0]:
                latest[name] = (timestamp, size, objects)
            elif timestamp == previous[0] and (size, objects) != previous[1:]:
                complete = False
                latest[name] = (timestamp, max(size, previous[1]), max(objects, previous[2]))
        # Never turn missing/empty storage observations into confirmed zero usage.
        if bucket not in latest:
            complete = False
        result["storage_bytes"] = sum(row[1] for row in latest.values())
        result["object_count"] = sum(row[2] for row in latest.values())
        result["bucket_storage_bytes"] = latest[bucket][1] if bucket in latest else 0
        result["bucket_storage_observed_at"] = _iso(latest[bucket][0]) if bucket in latest else None
        result["complete"] = complete
        result["unknown_actions"] = sorted(unknown_actions)
        return result

    def close(self) -> None:
        self.session.close()
