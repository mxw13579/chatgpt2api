from __future__ import annotations

import io
import json
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, IMAGE_STORAGE_LOCK, config
from services.r2_client import CloudflareR2Client, R2Error
from utils.log import logger

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
R2_USAGE_FILE = DATA_DIR / "r2_usage.json"
IMAGE_INDEX_LOCK = IMAGE_STORAGE_LOCK
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

    def _load_document(self) -> dict:
        document = _read_json_object(self.index_file)
        items = document.get("items")
        if not isinstance(items, dict) or any(not isinstance(value, dict) for value in items.values()):
            raise ImageStorageError("image index is invalid")
        return document

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

    def _pick_account(self, settings: dict, now: datetime, excluded: set[str] = frozenset()) -> dict | None:
        """Enabled account with the lowest share of its monthly upload limit used."""
        usage = self._load_usage()
        month = self._month(now)
        candidates = []
        for account in settings.get("r2_accounts", []):
            if not account["enabled"] or account["id"] in excluded:
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
                "image_count": sum(1 for item in items if item["account_id"] == account["id"]),
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
            occupied = {item["account_id"] for item in self._load_document()["items"].values()}
            for account_id in occupied:
                if account_id not in new or account_id not in old:
                    raise ValueError("R2 account has outstanding images and cannot be removed")
                if any(old[account_id].get(key) != new[account_id].get(key) for key in ("account_id", "bucket")):
                    raise ValueError("R2 account identity/bucket has outstanding images and cannot be changed")
                if any(not _clean(new[account_id].get(key)) for key in ("access_key_id", "secret_access_key")):
                    raise ValueError("R2 account credentials are required while images remain")

    def require_upload_available(self) -> None:
        with self._index_lock:
            if self._pick_account(self.settings(), _utc_now()) is None:
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

    def public_url(self, rel: str) -> str:
        safe_rel = _safe_relative_path(rel)
        with self._index_lock:
            item = self._load_document()["items"].get(safe_rel)
            if not item:
                raise HTTPException(status_code=404, detail="image not found")
            accounts = {account["id"]: account for account in self.settings().get("r2_accounts", [])}
            return self._r2_public_url(item, accounts)

    def resolve_reference(self, ref: dict) -> dict:
        result = {key: ref[key] for key in ("image_id", "account_id", "expires_at") if key in ref}
        result["url"] = ""
        try:
            with self._index_lock:
                document = self._load_document()
                item = document["items"].get(_safe_relative_path(str(ref.get("image_id") or "")))
                if not item or (ref.get("account_id") and ref["account_id"] != item["account_id"]):
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

    def save(self, image_data: bytes) -> StoredImage:
        extension, mime, dimensions = _r2_image_info(image_data)
        failed: set[str] = set()
        while True:
            with self._index_lock:
                settings = self.settings()
                now = _utc_now()
                document = self._load_document()
                account = self._pick_account(settings, now, failed)
                if account is None:
                    # 每个可用账号都试过且都失败时，报上传失败；否则是没有可用账号。
                    raise ImageStorageError(
                        "R2 image upload failed" if failed
                        else "R2 image storage has no enabled account with remaining monthly uploads"
                    )
                rel = f"images/{now:%Y/%m/%d}/{uuid.uuid4().hex}{extension}"
                expires = now + timedelta(minutes=int(settings.get("retention_minutes", 60)))
                item = {
                    "rel": rel, "path": rel, "name": Path(rel).name, "date": now.strftime("%Y-%m-%d"),
                    "storage": "r2", "account_id": account["id"], "object_key": rel, "image_id": rel,
                    "size": len(image_data), "mime": mime, "created_at": now.isoformat(),
                    "expires_at": expires.isoformat(), "pending": True,
                }
                if dimensions:
                    item["width"], item["height"] = dimensions
                document["items"][rel] = item
                _write_json_object(self.index_file, document)
                self._count_upload(account["id"], now)
                self._uploading.add(rel)
            try:
                client = CloudflareR2Client({**account, "prefix": "images"})
                try:
                    client.upload_bytes(
                        rel, image_data, content_type=mime,
                        metadata={"expires-at": expires.isoformat()},
                        cache_control=f"public, max-age=0, s-maxage={int(settings.get('cache_ttl_seconds', 60))}, must-revalidate",
                    )
                except R2Error as exc:
                    # 这个账号传不上去（如密钥失效返回 403）：记下原因，换下一个账号。
                    logger.warning({"event": "r2_image_upload_failed", "account": account["name"], "error": str(exc)})
                    failed.add(account["id"])
                    continue
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

    def _delete_r2(self, rel: str, client: CloudflareR2Client) -> bool:
        with self._index_lock:
            # Recheck each key: earlier deletes have released the lock for I/O.
            item = self._load_document()["items"].get(rel)
            if not item:
                return False
            if rel in self._uploading:
                raise ImageStorageError("R2 image upload is still in progress")
        try:
            client.delete_object(item["object_key"])
        except R2Error:
            with self._index_lock:
                document = self._load_document()
                if rel in document["items"]:
                    document["items"][rel].update(delete_error="R2 image delete failed", delete_failed_at=_now_iso())
                    _write_json_object(self.index_file, document)
            raise ImageStorageError("R2 image delete failed") from None
        with self._index_lock:
            document = self._load_document()
            document["items"].pop(rel, None)
            _write_json_object(self.index_file, document)
        return True

    def cleanup_expired(self) -> dict[str, int]:
        now = _utc_now()
        with self._index_lock:
            expired = [item for item in self._load_document()["items"].values()
                       if (_parse_time(item.get("expires_at")) or now) <= now]
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
                          if (_parse_time(item.get("expires_at")) or now) <= now)
        return {"removed": removed, "failed": failed, "pending": pending}


image_storage_service = ImageStorageService()
