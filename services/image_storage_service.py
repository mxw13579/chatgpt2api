from __future__ import annotations

import hashlib
import io
import json
import random
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from curl_cffi import requests
from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, IMAGE_STORAGE_LOCK, config
from services.r2_client import CloudflareR2Client, R2Error

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
IMAGE_INDEX_LOCK = IMAGE_STORAGE_LOCK
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
R2_CLEANUP_BATCH_SIZE = 100


class ImageStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredImage:
    rel: str
    url: str
    storage: str
    size: int
    image_id: str = ""
    account_id: str = ""
    expires_at: str = ""


def _clean(value: object) -> str:
    return str(value or "").strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utc_now().isoformat()


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _cycle_start(account: dict, now: datetime) -> datetime:
    start = now.replace(day=int(account.get("billing_cycle_day", 1)), hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        previous_month = start.replace(day=1) - timedelta(days=1)
        start = previous_month.replace(day=start.day)
    return start


def _r2_image_info(payload: bytes) -> tuple[str, str, tuple[int, int] | None]:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            extension, mime = {
                "PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
                "WEBP": (".webp", "image/webp"), "GIF": (".gif", "image/gif"),
                "AVIF": (".avif", "image/avif"),
            }.get(image.format, (".png", "image/png"))
            return extension, mime, image.size
    except (OSError, ValueError):
        return ".png", "image/png", None


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _image_dimensions(payload: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception:
        return None


def _is_image_rel(path: str) -> bool:
    try:
        safe_rel = _safe_relative_path(path)
    except HTTPException:
        return False
    return Path(safe_rel).suffix.lower() in IMAGE_EXTENSIONS


def _local_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    # config.images_dir creates the directory merely by reading the property.
    root = (DATA_DIR / "images").resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    return path


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"items": {}, "r2_usage": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        raise ImageStorageError("image index is invalid") from None
    if not isinstance(data, dict):
        raise ImageStorageError("image index is invalid")
    return data


def _write_json_object(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


class WebDAVClient:
    def __init__(self, settings: dict[str, object]):
        self.url = _clean(settings.get("webdav_url")).rstrip("/")
        self.username = _clean(settings.get("webdav_username"))
        self.password = _clean(settings.get("webdav_password"))
        self.root_path = _clean(settings.get("webdav_root_path")).strip("/")
        self.session = requests.Session()

    def _auth_kwargs(self) -> dict[str, object]:
        return {"auth": (self.username, self.password)} if self.username or self.password else {}

    def _request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=30, **self._auth_kwargs(), **kwargs)
        if response.status_code >= 400 and not (method == "MKCOL" and response.status_code in {405}):
            raise ImageStorageError(f"WebDAV {method} failed: HTTP {response.status_code}")
        return response

    def remote_url(self, rel: str = "") -> str:
        parts = [part for part in [self.root_path, _safe_relative_path(rel) if rel else ""] if part]
        encoded = "/".join(quote(part, safe="") for item in parts for part in item.split("/") if part)
        return f"{self.url}/{encoded}" if encoded else self.url

    def ensure_dirs(self, rel: str) -> None:
        parts = [part for part in [self.root_path, Path(_safe_relative_path(rel)).parent.as_posix()] if part and part != "."]
        current = self.url
        for item in "/".join(parts).split("/"):
            if not item:
                continue
            current = f"{current}/{quote(item, safe='')}"
            response = self.session.request("MKCOL", current, timeout=30, **self._auth_kwargs())
            if response.status_code in {201, 405}:
                continue
            if response.status_code >= 400:
                raise ImageStorageError(f"WebDAV MKCOL failed: HTTP {response.status_code}")

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        self.ensure_dirs(rel)
        url = self.remote_url(rel)
        self._request("PUT", url, data=payload, headers={"Content-Type": content_type})
        return url

    def get(self, rel: str) -> bytes:
        response = self._request("GET", self.remote_url(rel))
        return bytes(response.content)

    def delete(self, rel: str) -> bool:
        response = self.session.request("DELETE", self.remote_url(rel), timeout=30, **self._auth_kwargs())
        if response.status_code in {200, 202, 204, 404}:
            return response.status_code != 404
        raise ImageStorageError(f"WebDAV DELETE failed: HTTP {response.status_code}")

    def test(self) -> dict[str, object]:
        if not self.url:
            return {"ok": False, "status": 0, "error": "WebDAV URL is required"}
        if urlparse(self.url).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid WebDAV URL"}
        test_rel = ".chatgpt2api_webdav_test.txt"
        try:
            self.put(test_rel, b"chatgpt2api webdav test\n", content_type="text/plain")
            self.delete(test_rel)
            return {"ok": True, "status": 200, "error": None}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}
        finally:
            self.session.close()


class ImageStorageService:
    def __init__(self, index_file: Path = IMAGE_INDEX_FILE):
        self.index_file = index_file
        self._index_lock = IMAGE_INDEX_LOCK
        # Prevent an explicit delete/cleanup racing a PUT still in flight. Restarted
        # pending records are not in this set and remain eligible for expiry cleanup.
        self._uploading: set[str] = set()

    def settings(self) -> dict[str, object]:
        return config.get_image_storage_settings()

    def mode(self) -> str:
        return _clean(self.settings().get("mode")) or "local"

    def _load_document(self) -> dict:
        document = _read_json_object(self.index_file)
        if "items" not in document:
            raise ImageStorageError("image index is invalid")
        for field in ("items", "r2_usage"):
            mapping = document.setdefault(field, {})
            if not isinstance(mapping, dict) or any(not isinstance(value, dict) for value in mapping.values()):
                raise ImageStorageError("image index is invalid")
        return document

    def _load_index(self) -> dict[str, dict[str, object]]:
        with self._index_lock:
            return self._load_document()["items"]

    def _load_clean_index(self) -> dict[str, dict[str, object]]:
        return {rel: item for rel, item in self._load_index().items() if item.get("storage") == "r2" or _is_image_rel(rel)}

    def _save_index(self, items: dict[str, dict[str, object]]) -> None:
        # Legacy callers must preserve the concurrently maintained R2 state.
        with self._index_lock:
            document = self._load_document()
            document["items"] = items
            _write_json_object(self.index_file, document)

    def account(self, account_id: str) -> dict:
        for account in self.settings().get("r2_accounts", []):
            if account["id"] == account_id:
                return deepcopy(account)
        raise ValueError("R2 account not found")

    def get_r2_usage(self, account_id: str) -> dict:
        self.account(account_id)
        with self._index_lock:
            return deepcopy(self._load_document()["r2_usage"].get(account_id, {}))

    @staticmethod
    def _increment_a(state: dict, account: dict, count: int, now: datetime) -> None:
        cycle = _cycle_start(account, now).isoformat()
        if state.get("a_observed_cycle_start") != cycle or state.get("a_observed_cf_account_id") != account["account_id"]:
            state["a_observed_cycle_start"] = cycle
            state["a_observed_cf_account_id"] = account["account_id"]
            state["a_observed_cycle_operations"] = 0
        state["a_observed_cycle_operations"] = int(state.get("a_observed_cycle_operations", 0)) + count
        state["a_local"] = max(int(state.get("a_local", 0)), int(state.get("cloud_a", 0))) + count

    def update_r2_usage(self, account_id: str, updates: dict) -> dict:
        """Merge management state. Use a_local_increment for atomic Class A debits.

        Absolute a_local values are merged with max, never used to reset a cycle.
        Only record_usage_snapshot may reset that counter after a fresh full query.
        """
        account = self.account(account_id)
        changes = deepcopy(updates)
        increment = changes.pop("a_local_increment", 0)
        if isinstance(increment, bool) or not isinstance(increment, int) or increment < 0:
            raise ValueError("invalid R2 Class A increment")
        debit = increment
        with self._index_lock:
            document = self._load_document()
            state = document["r2_usage"].setdefault(account_id, {})
            absolute = changes.pop("a_local", None)
            baseline = max(int(state.get("a_local", 0)), int(state.get("cloud_a", 0)))
            if absolute is not None:
                if isinstance(absolute, bool) or not isinstance(absolute, int) or absolute < 0:
                    raise ValueError("invalid R2 Class A counter")
                increment += max(0, absolute - baseline)
            state.update(changes)
            if "verified_domains" in changes:
                # Reusing an empty account's internal ID for another CF account /
                # bucket must not reuse the previous protection verification.
                state.update(protection_cf_account_id=account.get("account_id"), protection_bucket=account.get("bucket"))
            if debit and max(baseline, int(state.get("cloud_a", 0))) + increment > int(account.get("a_budget", 800000)):
                raise ImageStorageError("R2 Class A budget exhausted")
            if increment:
                state["a_local"] = baseline
                self._increment_a(state, account, increment, _utc_now())
            elif absolute is not None:
                state["a_local"] = max(baseline, absolute)
            _write_json_object(self.index_file, document)
            return deepcopy(state)

    def record_usage_snapshot(self, account_id: str, result: dict) -> dict:
        """Atomically accept CloudflareAPI.query_usage's normalized result.

        cloud_* are never added to the complete a_local counter. The two
        a_observed_cycle_* fields retain operations issued during a rollover
        query, including a concurrent connection test. capacity_snapshot_at is
        the query end, not its completion time; uploads crossing it remain delta.
        Incomplete/stale responses retain old counts/timestamps and cannot reset A.
        """
        account = self.account(account_id)
        now = _utc_now()
        start, end = _parse_time(result.get("period_start")), _parse_time(result.get("period_end"))
        fields = {
            "a_operations": "cloud_a", "b_operations": "cloud_b",
            "bucket_a_operations": "cloud_bucket_a", "bucket_b_operations": "cloud_bucket_b",
            "storage_bytes": "cloud_bytes", "bucket_storage_bytes": "cloud_bucket_bytes",
            "object_count": "cloud_object_count",
        }
        counts_valid = all(type(result.get(key)) is int and result[key] >= 0 for key in fields)
        fresh = end is not None and 0 <= (now - end).total_seconds() <= int(self.settings().get("usage_max_age_seconds", 900))
        valid = (account.get("billing_cycle_confirmed") is True
                 and result.get("complete") is True and not result.get("unknown_actions") and counts_valid
                 and start == _cycle_start(account, now) and fresh and end >= start)
        with self._index_lock:
            document = self._load_document()
            state = document["r2_usage"].setdefault(account_id, {})
            if not valid:
                state.update(complete=False, error="R2 usage snapshot incomplete or stale")
            elif (_parse_time(state.get("period_end")) or datetime.min.replace(tzinfo=timezone.utc)) <= end:
                new_cycle = (_parse_time(state.get("period_start")) != start
                             or state.get("usage_cf_account_id") != account["account_id"])
                observed = (int(state.get("a_observed_cycle_operations", 0))
                            if _parse_time(state.get("a_observed_cycle_start")) == start
                            and state.get("a_observed_cf_account_id") == account["account_id"] else 0)
                local = observed if new_cycle else int(state.get("a_local", 0))
                query_count = int(result.get("a_query_count", observed)) if _parse_time(result.get("a_query_cycle")) == start else 0
                concurrent = max(0, observed - query_count) if "a_query_count" in result else 0
                state.update({target: result[source] for source, target in fields.items()})
                state.update(
                    a_local=max(local, result["a_operations"] + concurrent), fetched_at=now.isoformat(),
                    period_start=start.isoformat(), period_end=end.isoformat(),
                    last_successful_usage_start=start.isoformat(),
                    capacity_snapshot_at=result.get("bucket_storage_observed_at", end.isoformat()),
                    usage_cf_account_id=account["account_id"], usage_bucket=account["bucket"],
                    complete=True, error="", unknown_actions=[],
                )
            _write_json_object(self.index_file, document)
            return deepcopy(state)

    @staticmethod
    def _usage_view(document: dict, account_id: str) -> dict:
        state = deepcopy(document["r2_usage"].get(account_id, {}))
        local_bytes = pending_bytes = image_count = pending_count = upload_delta = 0
        snapshot_at = _parse_time(state.get("capacity_snapshot_at"))
        for item in document["items"].values():
            if item.get("storage") != "r2" or item.get("account_id") != account_id:
                continue
            size = int(item["size"])
            if item.get("pending"):
                pending_bytes += size
                pending_count += 1
            else:
                local_bytes += size
                image_count += 1
            uploaded_at = _parse_time(item.get("uploaded_at") or item.get("created_at"))
            # Unknown PUT outcomes cannot be reconciled to aggregate cloud stats.
            if item.get("pending") or snapshot_at is None or uploaded_at is None or uploaded_at > snapshot_at:
                upload_delta += size
        cloud_bytes = int(state.get("cloud_bytes", 0))
        other_bucket_bytes = max(0, cloud_bytes - int(state.get("cloud_bucket_bytes", 0)))
        state.update(
            local_bytes=local_bytes, pending_bytes=pending_bytes, image_count=image_count,
            pending_count=pending_count, upload_delta_bytes=upload_delta,
            # Aggregate analytics can lag even before the query cutoff. Keep a
            # separate lower bound for other buckets + all known image bytes,
            # without adding the image bucket's already-counted baseline twice.
            effective_bytes=max(cloud_bytes + upload_delta, other_bucket_bytes + local_bytes + pending_bytes),
            effective_a=max(int(state.get("a_local", 0)), int(state.get("cloud_a", 0))),
        )
        return state

    def account_usage(self, account_id: str) -> dict:
        self.account(account_id)
        with self._index_lock:
            return self._usage_view(self._load_document(), account_id)

    def validate_settings_change(self, new_settings: dict) -> None:
        old = {account["id"]: account for account in self.settings().get("r2_accounts", [])}
        new = {account["id"]: account for account in new_settings.get("r2_accounts", [])}
        with self._index_lock:
            occupied = {item["account_id"] for item in self._load_document()["items"].values() if item.get("storage") == "r2"}
            for account_id in occupied:
                if account_id not in new or account_id not in old:
                    raise ValueError("R2 account has outstanding images and cannot be removed")
                if any(old[account_id].get(key) != new[account_id].get(key) for key in ("account_id", "bucket")):
                    raise ValueError("R2 account identity/bucket has outstanding images and cannot be changed")
                if any(not _clean(new[account_id].get(key)) for key in ("access_key_id", "secret_access_key", "api_token")):
                    raise ValueError("R2 account credentials are required while images remain")

    @staticmethod
    def _active_domain(account: dict) -> str:
        for domain in account.get("domains", []):
            if domain["id"] == account.get("active_domain_id") and domain.get("enabled"):
                return str(domain["domain"])
        return ""

    def _account_status(self, account: dict, usage: dict, settings: dict, now: datetime, size: int = 0) -> str:
        if usage.get("access_paused"):
            return "access_paused"
        if not account.get("enabled"):
            return "disabled"
        if not all(_clean(account.get(key)) for key in ("account_id", "bucket", "access_key_id", "secret_access_key", "api_token")):
            return "unconfigured"
        if not account.get("billing_cycle_confirmed"):
            return "cycle_unconfirmed"
        verified = set(usage.get("verified_domains", []))
        enabled = {domain["domain"] for domain in account.get("domains", []) if domain.get("enabled")}
        active = self._active_domain(account)
        if (not active or active not in verified or not enabled.issubset(verified)
                or usage.get("managed_disabled") is not True or usage.get("domains_checked") is not True
                or usage.get("domains_safe") is not True
                or usage.get("protection_cf_account_id") != account["account_id"]
                or usage.get("protection_bucket") != account["bucket"]):
            return "domains_unprotected"
        fetched = _parse_time(usage.get("fetched_at"))
        if (usage.get("complete") is not True or fetched is None
                or usage.get("usage_cf_account_id") != account["account_id"] or usage.get("usage_bucket") != account["bucket"]
                or not 0 <= (now - fetched).total_seconds() <= int(settings.get("usage_max_age_seconds", 900))
                or _parse_time(usage.get("period_start")) != _cycle_start(account, now)):
            return "usage_stale"
        if usage["effective_bytes"] >= int(account.get("capacity_bytes", 8000000000)) or usage["effective_bytes"] + size > int(account.get("capacity_bytes", 8000000000)):
            return "capacity_exhausted"
        if usage["effective_a"] >= int(account.get("a_budget", 800000)):
            return "a_exhausted"
        if int(usage.get("cloud_b", 0)) >= int(account.get("b_budget", 8000000)):
            return "b_exhausted"
        return "eligible"

    def account_status(self, account_id: str) -> str:
        with self._index_lock:
            return self._account_status(self.account(account_id), self.account_usage(account_id), self.settings(), _utc_now())

    def _eligible_accounts(self, document: dict, settings: dict, size: int, now: datetime) -> list[dict]:
        return [account for account in settings.get("r2_accounts", [])
                if self._account_status(account, self._usage_view(document, account["id"]), settings, now, size) == "eligible"]

    def require_upload_available(self, size: int = 0) -> None:
        with self._index_lock:
            settings = self.settings()
            if settings.get("mode") != "r2":
                return
            if not self._eligible_accounts(self._load_document(), settings, size, _utc_now()):
                raise ImageStorageError("R2 image storage has no eligible account or available budget")

    def metadata(self, rel: str) -> dict | None:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            return deepcopy(self._load_document()["items"].get(safe_rel))

    @staticmethod
    def _require_readable(item: dict, usage: dict) -> None:
        if item.get("pending"):
            raise HTTPException(status_code=404, detail="image not found")
        expires = _parse_time(item.get("expires_at"))
        if expires is None or expires <= _utc_now():
            raise HTTPException(status_code=410, detail="image expired")
        if usage.get("access_paused"):
            raise HTTPException(status_code=403, detail="R2 image access paused")

    def _r2_public_url(self, item: dict, document: dict) -> str:
        self._require_readable(item, document["r2_usage"].get(item["account_id"], {}))
        try:
            domain = self._active_domain(self.account(item["account_id"]))
        except ValueError:
            raise HTTPException(status_code=404, detail="image account unavailable") from None
        if not domain:
            raise HTTPException(status_code=403, detail="R2 public domain unavailable")
        return f"https://{domain}/{quote(item['object_key'], safe='/')}"

    def public_url(self, rel: str, base_url: str | None = None) -> str:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            document = self._load_document()
            item = document["items"].get(safe_rel)
            if item and item.get("storage") == "r2":
                return self._r2_public_url(item, document)
            if not _is_image_rel(safe_rel) or not ((item and item.get("webdav")) or _local_image_path(safe_rel).is_file()):
                raise HTTPException(status_code=404, detail="image not found")
        return self._public_url(safe_rel, base_url)

    def reference_for_url(self, url: str) -> dict | None:
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
                return None
            key = _safe_relative_path(unquote(parsed.path))
        except (ValueError, HTTPException):
            return None
        if not key.startswith("images/"):
            return None
        item = self.metadata(key)
        if not item or item.get("storage") != "r2" or item.get("pending") or item.get("object_key") != key:
            return None
        try:
            hosts = {domain["domain"].lower() for domain in self.account(item["account_id"]).get("domains", [])}
        except ValueError:
            return None
        if parsed.hostname not in hosts:
            return None
        return {key: item[key] for key in ("image_id", "account_id", "expires_at")}

    def resolve_reference(self, ref: dict) -> dict:
        result = {key: ref[key] for key in ("image_id", "account_id", "expires_at") if key in ref}
        result["url"] = ""
        try:
            with self._index_lock:
                document = self._load_document()
                item = document["items"].get(_safe_relative_path(str(ref.get("image_id") or "")))
                if (not item or item.get("storage") != "r2"
                        or (ref.get("account_id") and ref["account_id"] != item["account_id"])):
                    return {**result, "expired": True}
                result.update({key: item[key] for key in ("image_id", "account_id", "expires_at")})
                result["url"] = self._r2_public_url(item, document)
                return result
        except HTTPException as exc:
            result["expired" if exc.status_code in {404, 410} else "unavailable"] = True
            return result

    def _r2_client(self, account_id: str) -> CloudflareR2Client:
        return CloudflareR2Client({**self.account(account_id), "prefix": "images"})

    def _save_r2(self, image_data: bytes) -> StoredImage:
        extension, mime, dimensions = _r2_image_info(image_data)
        with self._index_lock:
            settings, now = self.settings(), _utc_now()
            document = self._load_document()
            candidates = self._eligible_accounts(document, settings, len(image_data), now)
            if not candidates:
                raise ImageStorageError("R2 image storage has no eligible account or available budget")
            account = random.choice(candidates)
            rel = f"images/{now:%Y/%m/%d}/{uuid.uuid4().hex}{extension}"
            expires = now + timedelta(minutes=int(settings.get("retention_minutes", 60)))
            item = {
                "rel": rel, "path": rel, "name": Path(rel).name, "date": now.strftime("%Y-%m-%d"),
                "storage": "r2", "account_id": account["id"], "object_key": rel, "image_id": rel,
                "size": len(image_data), "mime": mime, "created_at": now.isoformat(),
                "expires_at": expires.isoformat(), "pending": True, "local": False, "webdav": False,
            }
            if dimensions:
                item["width"], item["height"] = dimensions
            document["items"][rel] = item
            self._increment_a(document["r2_usage"].setdefault(account["id"], {}), account, 1, now)
            _write_json_object(self.index_file, document)
            self._uploading.add(rel)
        try:
            client = CloudflareR2Client({**account, "prefix": "images"})
            try:
                client.upload_bytes(
                    rel, image_data, content_type=mime,
                    metadata={"expires-at": expires.isoformat()},
                    cache_control=f"public, max-age=0, s-maxage={int(settings.get('cache_ttl_seconds', 60))}, must-revalidate",
                )
            except R2Error:
                raise ImageStorageError("R2 image upload failed") from None
            finally:
                client.close()
            with self._index_lock:
                document = self._load_document()
                document["items"][rel].update(pending=False, uploaded_at=_now_iso())
                _write_json_object(self.index_file, document)
            return StoredImage(rel=rel, url=self.public_url(rel), storage="r2", size=len(image_data),
                               image_id=rel, account_id=account["id"], expires_at=expires.isoformat())
        finally:
            with self._index_lock:
                self._uploading.discard(rel)

    def _public_url(self, rel: str, base_url: str | None = None) -> str:
        settings = self.settings()
        public_base_url = _clean(settings.get("public_base_url"))
        if public_base_url:
            return f"{public_base_url.rstrip('/')}/{_safe_relative_path(rel)}"
        return f"{(base_url or config.base_url).rstrip('/')}/images/{_safe_relative_path(rel)}"

    def make_relative_path(self, image_data: bytes) -> str:
        file_hash = hashlib.md5(image_data).hexdigest()
        filename = f"{int(time.time())}_{file_hash}.png"
        relative_dir = Path(time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
        return f"{relative_dir.as_posix()}/{filename}"

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        mode = self.mode()
        if mode == "r2":
            return self._save_r2(image_data)
        config.cleanup_old_images()
        rel = self.make_relative_path(image_data)
        if mode not in {"local", "webdav", "both"}:
            mode = "local"
        stored_local = False
        stored_webdav = False
        remote_url = ""

        if mode in {"local", "both"}:
            path = _local_image_path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image_data)
            stored_local = True

        if mode in {"webdav", "both"}:
            remote_url = WebDAVClient(self.settings()).put(rel, image_data)
            stored_webdav = True

        dimensions = _image_dimensions(image_data)
        item = {
            "rel": rel,
            "path": rel,
            "name": Path(rel).name,
            "date": "-".join(rel.split("/")[:3]),
            "size": len(image_data),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "storage": "both" if stored_local and stored_webdav else ("webdav" if stored_webdav else "local"),
            "local": stored_local,
            "webdav": stored_webdav,
            "remote_url": remote_url,
        }
        if dimensions:
            item["width"], item["height"] = dimensions
        with self._index_lock:
            items = self._load_clean_index()
            items[rel] = item
            self._save_index(items)
        return StoredImage(rel=rel, url=self._public_url(rel, base_url), storage=str(item["storage"]), size=len(image_data))

    def get_bytes(self, rel: str) -> bytes:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            document = self._load_document()
            item = document["items"].get(safe_rel, {})
            if item.get("storage") == "r2":
                self._require_readable(item, document["r2_usage"].get(item["account_id"], {}))
        if item.get("storage") == "r2":
            client = self._r2_client(item["account_id"])
            try:
                return client.download_bytes(item["object_key"])
            except R2Error:
                raise ImageStorageError("R2 image download failed") from None
            finally:
                client.close()
        if not _is_image_rel(safe_rel):
            raise HTTPException(status_code=404, detail="image not found")
        path = _local_image_path(safe_rel)
        if path.is_file():
            return path.read_bytes()
        if item.get("webdav"):
            return WebDAVClient(self.settings()).get(safe_rel)
        raise HTTPException(status_code=404, detail="image not found")

    def exists(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        item = self.metadata(safe_rel) or {}
        if item.get("storage") == "r2":
            expires = _parse_time(item.get("expires_at"))
            return not item.get("pending") and expires is not None and expires > _utc_now()
        return _is_image_rel(safe_rel) and (bool(item.get("webdav")) or _local_image_path(safe_rel).is_file())

    def has_local(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        if (self.metadata(safe_rel) or {}).get("storage") == "r2":
            return False
        return _is_image_rel(safe_rel) and _local_image_path(safe_rel).is_file()

    def list_items(self, base_url: str, start_date: str = "", end_date: str = "") -> list[dict[str, object]]:
        with self._index_lock:
            indexed = self._load_clean_index()
            root = DATA_DIR / "images"
            changed = False
            for path in (() if self.mode() == "r2" else root.rglob("*")):
                if not path.is_file() or not _is_image_rel(path.name):
                    continue
                rel = path.relative_to(root).as_posix()
                if rel in indexed:
                    continue
                dimensions = None
                try:
                    dimensions = _image_dimensions(path.read_bytes())
                except Exception:
                    dimensions = None
                indexed[rel] = {
                    "rel": rel,
                    "path": rel,
                    "name": path.name,
                    "date": "-".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"),
                    "size": path.stat().st_size,
                    "created_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "storage": "local",
                    "local": True,
                    "webdav": False,
                    **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                }
                changed = True

            items: list[dict[str, object]] = []
            document = self._load_document()
            for rel, item in list(indexed.items()):
                if item.get("storage") == "r2":
                    if item.get("pending"):
                        continue
                    day = str(item.get("date") or "")
                    if (start_date and day < start_date) or (end_date and day > end_date):
                        continue
                    visible = {**item, "rel": rel, "path": rel, "url": ""}
                    try:
                        visible["url"] = self._r2_public_url(item, document)
                    except HTTPException as exc:
                        visible["expired" if exc.status_code == 410 else "unavailable"] = True
                    items.append(visible)
                    continue
                if not _is_image_rel(rel):
                    indexed.pop(rel, None)
                    changed = True
                    continue
                local = _local_image_path(rel).is_file()
                webdav = bool(item.get("webdav"))
                if not local and not webdav:
                    indexed.pop(rel, None)
                    changed = True
                    continue
                storage = "both" if local and webdav else ("webdav" if webdav else "local")
                if item.get("local") != local or item.get("storage") != storage:
                    item = {
                        **item,
                        "local": local,
                        "storage": storage,
                    }
                    indexed[rel] = item
                    changed = True
                day = str(item.get("date") or "")
                if start_date and day < start_date:
                    continue
                if end_date and day > end_date:
                    continue
                items.append({
                    **item,
                    "rel": rel,
                    "path": rel,
                    "url": self._public_url(rel, base_url),
                })
            if changed:
                self._save_index(indexed)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def _delete_r2(self, rel: str, client: CloudflareR2Client | None = None) -> bool:
        with self._index_lock:
            item = self._load_document()["items"].get(rel)
            if not item:
                return False
            if rel in self._uploading:
                raise ImageStorageError("R2 image upload is still in progress")
        own_client = client is None
        if own_client:
            client = self._r2_client(item["account_id"])
        try:
            client.delete_object(item["object_key"])
        except R2Error:
            with self._index_lock:
                document = self._load_document()
                if rel in document["items"]:
                    document["items"][rel].update(delete_error="R2 image delete failed", delete_failed_at=_now_iso())
                    _write_json_object(self.index_file, document)
            raise ImageStorageError("R2 image delete failed") from None
        finally:
            if own_client:
                client.close()
        with self._index_lock:
            document = self._load_document()
            document["items"].pop(rel, None)
            _write_json_object(self.index_file, document)
        return True

    def cleanup_expired(self) -> dict[str, int]:
        now = _utc_now()
        with self._index_lock:
            expired = [item for item in self._load_document()["items"].values()
                       if item.get("storage") == "r2" and (_parse_time(item.get("expires_at")) or now) <= now]
            expired.sort(key=lambda item: str(item.get("expires_at") or ""))
            targets = [item for item in expired if item["image_id"] not in self._uploading][:R2_CLEANUP_BATCH_SIZE]
        removed = failed = 0
        clients: dict[str, CloudflareR2Client] = {}
        try:
            for item in targets:
                try:
                    account_id = item["account_id"]
                    if account_id not in clients:
                        clients[account_id] = self._r2_client(account_id)
                    removed += int(self._delete_r2(item["image_id"], clients[account_id]))
                except (ImageStorageError, R2Error, ValueError):
                    failed += 1
        finally:
            for client in clients.values():
                client.close()
        with self._index_lock:
            pending = sum(1 for item in self._load_document()["items"].values()
                          if item.get("storage") == "r2" and (_parse_time(item.get("expires_at")) or now) <= now)
        return {"removed": removed, "failed": failed, "pending": pending}

    def delete(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        if (self.metadata(safe_rel) or {}).get("storage") == "r2":
            return self._delete_r2(safe_rel)
        removed = False
        path = _local_image_path(safe_rel)
        if path.is_file():
            path.unlink()
            removed = True
        with self._index_lock:
            items = self._load_clean_index()
            item = items.get(safe_rel, {})
            if item.get("webdav"):
                try:
                    removed = WebDAVClient(self.settings()).delete(safe_rel) or removed
                except ImageStorageError:
                    if not removed:
                        raise
            if safe_rel in items:
                items.pop(safe_rel, None)
                self._save_index(items)
        return removed

    def sync_all(self) -> dict[str, int]:
        settings = self.settings()
        if self.mode() not in {"webdav", "both"}:
            raise ImageStorageError("WebDAV 图片存储未启用")
        uploaded = 0
        skipped = 0
        failed = 0
        with self._index_lock:
            items = self._load_clean_index()
            client = WebDAVClient(settings)
            root = DATA_DIR / "images"
            for path in sorted(root.rglob("*")):
                if not path.is_file() or not _is_image_rel(path.name):
                    continue
                rel = path.relative_to(root).as_posix()
                item = items.get(rel, {})
                if item.get("storage") == "r2" or item.get("webdav"):
                    skipped += 1
                    continue
                try:
                    payload = path.read_bytes()
                    remote_url = client.put(rel, payload)
                    dimensions = _image_dimensions(payload)
                    items[rel] = {
                        **item,
                        "rel": rel,
                        "path": rel,
                        "name": path.name,
                        "date": "-".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"),
                        "size": len(payload),
                        "created_at": str(item.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")),
                        "storage": "both",
                        "local": True,
                        "webdav": True,
                        "remote_url": remote_url,
                        **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                    }
                    uploaded += 1
                except Exception:
                    failed += 1
            self._save_index(items)
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    def test_webdav(self) -> dict[str, object]:
        return WebDAVClient(self.settings()).test()


image_storage_service = ImageStorageService()
