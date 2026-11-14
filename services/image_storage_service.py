from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from curl_cffi import requests
from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, IMAGE_STORAGE_LOCK, config
from services.r2_client import CloudflareR2Client, R2Error

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
R2_USAGE_FILE = DATA_DIR / "r2_usage.json"
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
        return {"items": {}}
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
    def __init__(self, index_file: Path = IMAGE_INDEX_FILE, usage_file: Path = R2_USAGE_FILE):
        self.index_file = index_file
        self.usage_file = usage_file
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
        items = document.get("items")
        if not isinstance(items, dict) or any(not isinstance(value, dict) for value in items.values()):
            raise ImageStorageError("image index is invalid")
        return document

    def _load_index(self) -> dict[str, dict[str, object]]:
        """Metadata selection snapshot; reconcile separately after local file I/O."""
        with self._index_lock:
            return self._load_document()["items"]

    @staticmethod
    def _clean_index(document: dict) -> dict[str, dict[str, object]]:
        document["items"] = {rel: item for rel, item in document["items"].items()
                             if item.get("storage") == "r2" or _is_image_rel(rel)}
        return document["items"]

    @staticmethod
    def _account(settings: dict, account_id: str) -> dict:
        for account in settings.get("r2_accounts", []):
            if account["id"] == account_id:
                return account
        raise ValueError("R2 account not found")

    def account(self, account_id: str) -> dict:
        return self._account(self.settings(), account_id)

    def _load_usage(self) -> dict:
        return _read_json_object(self.usage_file) if self.usage_file.exists() else {}

    @staticmethod
    def _month(now: datetime) -> str:
        return now.strftime("%Y-%m")

    def _monthly_uploads(self, usage: dict, account_id: str, month: str) -> int:
        entry = usage.get(account_id) or {}
        return int(entry.get("uploads", 0)) if entry.get("month") == month else 0

    def _count_upload(self, account_id: str, now: datetime) -> None:
        """Count one Class A operation before the request is sent; failures are not refunded."""
        with self._index_lock:
            usage = self._load_usage()
            month = self._month(now)
            usage[account_id] = {"month": month, "uploads": self._monthly_uploads(usage, account_id, month) + 1}
            _write_json_object(self.usage_file, usage)

    def _pick_account(self, settings: dict, now: datetime) -> dict | None:
        """Enabled account with the lowest share of its monthly upload limit used."""
        usage = self._load_usage()
        month = self._month(now)
        candidates = []
        for account in settings.get("r2_accounts", []):
            if not account["enabled"]:
                continue
            used = self._monthly_uploads(usage, account["id"], month)
            if used < account["monthly_upload_limit"]:
                candidates.append((used / account["monthly_upload_limit"], account))
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    def account_summaries(self) -> list[dict]:
        with self._index_lock:
            settings = self.settings()
            items = self._load_document()["items"].values()
            usage = self._load_usage()
            month = self._month(_utc_now())
            return [{
                "id": account["id"],
                "month": month,
                "monthly_uploads": self._monthly_uploads(usage, account["id"], month),
                "image_count": sum(1 for item in items if item.get("storage") == "r2" and item.get("account_id") == account["id"]),
            } for account in settings.get("r2_accounts", [])]

    def test_account(self, account_id: str) -> dict:
        account = self.account(account_id)
        self._count_upload(account_id, _utc_now())
        client = CloudflareR2Client({**account, "prefix": "images"})
        try:
            return client.test_connection()
        finally:
            client.close()

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
                if any(not _clean(new[account_id].get(key)) for key in ("access_key_id", "secret_access_key")):
                    raise ValueError("R2 account credentials are required while images remain")

    def require_upload_available(self) -> None:
        with self._index_lock:
            settings = self.settings()
            if settings.get("mode") == "r2" and self._pick_account(settings, _utc_now()) is None:
                raise ImageStorageError("R2 image storage has no enabled account with remaining monthly uploads")

    def metadata(self, rel: str) -> dict | None:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            return deepcopy(self._load_document()["items"].get(safe_rel))

    @staticmethod
    def _require_readable(item: dict) -> None:
        if item.get("pending"):
            raise HTTPException(status_code=404, detail="image not found")
        expires = _parse_time(item.get("expires_at"))
        if expires is None or expires <= _utc_now():
            raise HTTPException(status_code=410, detail="image expired")

    def _r2_public_url(self, item: dict, accounts: dict[str, dict]) -> str:
        self._require_readable(item)
        account = accounts.get(item["account_id"])
        if account is None:
            raise HTTPException(status_code=404, detail="image account unavailable")
        return f"{account['public_base_url']}/{quote(item['object_key'], safe='/')}"

    def public_url(self, rel: str, base_url: str | None = None) -> str:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            document = self._load_document()
            settings = self.settings()
            item = document["items"].get(safe_rel)
            if item and item.get("storage") == "r2":
                accounts = {account["id"]: account for account in settings.get("r2_accounts", [])}
                return self._r2_public_url(item, accounts)
            if not _is_image_rel(safe_rel) or not ((item and item.get("webdav")) or _local_image_path(safe_rel).is_file()):
                raise HTTPException(status_code=404, detail="image not found")
        return self._public_url(safe_rel, settings, base_url)

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
                accounts = {account["id"]: account for account in self.settings().get("r2_accounts", [])}
                result["url"] = self._r2_public_url(item, accounts)
                return result
        except HTTPException as exc:
            result["expired" if exc.status_code in {404, 410} else "unavailable"] = True
            return result

    def _r2_client(self, account_id: str) -> CloudflareR2Client:
        return CloudflareR2Client({**self.account(account_id), "prefix": "images"})

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        with self._index_lock:
            settings = self.settings()
            if settings.get("mode") == "r2":
                extension, mime, dimensions = _r2_image_info(image_data)
                now = _utc_now()
                document = self._load_document()
                account = self._pick_account(settings, now)
                if account is None:
                    raise ImageStorageError("R2 image storage has no enabled account with remaining monthly uploads")
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
                _write_json_object(self.index_file, document)
                self._count_upload(account["id"], now)
                self._uploading.add(rel)
        if settings.get("mode") != "r2":
            return self._save_legacy(image_data, settings, base_url)
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
                item = document["items"][rel]
                item.update(pending=False, uploaded_at=_now_iso())
                _write_json_object(self.index_file, document)
                accounts = {account["id"]: account for account in self.settings().get("r2_accounts", [])}
                url = self._r2_public_url(item, accounts)
            return StoredImage(rel=rel, url=url, storage="r2", size=len(image_data),
                               image_id=rel, account_id=account["id"], expires_at=expires.isoformat())
        finally:
            with self._index_lock:
                self._uploading.discard(rel)

    def _public_url(self, rel: str, settings: dict, base_url: str | None = None) -> str:
        public_base_url = _clean(settings.get("public_base_url"))
        if public_base_url:
            return f"{public_base_url.rstrip('/')}/{_safe_relative_path(rel)}"
        return f"{(base_url or config.base_url).rstrip('/')}/images/{_safe_relative_path(rel)}"

    def make_relative_path(self, image_data: bytes) -> str:
        file_hash = hashlib.md5(image_data).hexdigest()
        filename = f"{int(time.time())}_{file_hash}.png"
        relative_dir = Path(time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
        return f"{relative_dir.as_posix()}/{filename}"

    def _save_legacy(self, image_data: bytes, settings: dict, base_url: str | None) -> StoredImage:
        mode = settings.get("mode")
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
            remote_url = WebDAVClient(settings).put(rel, image_data)
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
            document = self._load_document()
            items = self._clean_index(document)
            items[rel] = item
            _write_json_object(self.index_file, document)
        return StoredImage(rel=rel, url=self._public_url(rel, settings, base_url), storage=str(item["storage"]), size=len(image_data))

    def get_bytes(self, rel: str) -> bytes:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            document = self._load_document()
            item = document["items"].get(safe_rel, {})
            if item.get("storage") == "r2":
                self._require_readable(item)
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
            document = self._load_document()
            indexed = document["items"]
            settings = self.settings()
            accounts = {account["id"]: account for account in settings.get("r2_accounts", [])}
            root = DATA_DIR / "images"
            changed = False
            for path in (() if settings.get("mode") == "r2" else root.rglob("*")):
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
            for rel, item in list(indexed.items()):
                if item.get("storage") == "r2":
                    if item.get("pending"):
                        continue
                    day = str(item.get("date") or "")
                    if (start_date and day < start_date) or (end_date and day > end_date):
                        continue
                    visible = {**item, "rel": rel, "path": rel, "url": ""}
                    try:
                        visible["url"] = self._r2_public_url(item, accounts)
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
                    "url": self._public_url(rel, settings, base_url),
                })
            if changed:
                _write_json_object(self.index_file, document)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def _delete_r2(self, rel: str, client: CloudflareR2Client | None = None, *, item: dict | None = None) -> bool:
        with self._index_lock:
            # Direct deletes already selected the item; expiry cleanup rechecks
            # each key because earlier deletes have released the lock for I/O.
            if item is None:
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
        with self._index_lock:
            document = self._load_document()
            item = document["items"].get(safe_rel, {})
            if item.get("storage") != "r2":
                removed = False
                path = _local_image_path(safe_rel)
                if path.is_file():
                    path.unlink()
                    removed = True
                items = self._clean_index(document)
                if item.get("webdav"):
                    try:
                        removed = WebDAVClient(self.settings()).delete(safe_rel) or removed
                    except ImageStorageError:
                        if not removed:
                            raise
                if safe_rel in items:
                    items.pop(safe_rel, None)
                    _write_json_object(self.index_file, document)
                return removed
        return self._delete_r2(safe_rel, item=item)

    def sync_all(self) -> dict[str, int]:
        settings = self.settings()
        if settings.get("mode") not in {"webdav", "both"}:
            raise ImageStorageError("WebDAV 图片存储未启用")
        uploaded = 0
        skipped = 0
        failed = 0
        with self._index_lock:
            document = self._load_document()
            items = self._clean_index(document)
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
            _write_json_object(self.index_file, document)
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    def test_webdav(self) -> dict[str, object]:
        return WebDAVClient(self.settings()).test()


image_storage_service = ImageStorageService()
