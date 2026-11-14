from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from PIL import Image, ImageOps

from services.config import DATA_DIR, config
from services.image_storage_service import image_storage_service
from services.image_tags_service import load_tags, remove_tags
from utils.log import logger

THUMBNAIL_SIZE = (320, 320)


def _r2_mode() -> bool:
    return config.get_image_storage_settings().get("mode") == "r2"


def _r2_image(relative_path: str) -> bool:
    return (image_storage_service.metadata(relative_path) or {}).get("storage") == "r2"


def _cleanup_empty_dirs(root: Path) -> None:
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _safe_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = (DATA_DIR / "images").resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return path


def get_image_response(relative_path: str) -> FileResponse | Response:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    rel = _safe_relative_path(relative_path)
    if _r2_image(rel):
        # Public routes must go through the protected domain, never our S3 credentials.
        return RedirectResponse(
            image_storage_service.public_url(rel),
            status_code=307,
            headers={**headers, "Cache-Control": "no-store"},
        )
    if image_storage_service.has_local(rel):
        return FileResponse(_safe_image_path(rel), headers=headers)
    return Response(content=image_storage_service.get_bytes(rel), media_type="image/png", headers=headers)


def _thumbnail_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    return DATA_DIR / "image_thumbnails" / f"{rel}.png"


def thumbnail_url(base_url: str, relative_path: str) -> str:
    """Build a legacy thumbnail URL; the listing already handles R2 separately."""
    rel = _safe_relative_path(relative_path)
    return f"{base_url.rstrip('/')}/image-thumbnails/{rel}"


def _image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None


def ensure_thumbnail(relative_path: str) -> Path:
    if _r2_mode() or _r2_image(relative_path):
        raise HTTPException(status_code=409, detail="local thumbnails are disabled for R2")
    target = _thumbnail_path(relative_path)
    source_mtime = 0.0
    source: Path | None = None
    if image_storage_service.has_local(relative_path):
        source = _safe_image_path(relative_path)
        source_mtime = source.stat().st_mtime
    if target.exists() and (not source_mtime or target.stat().st_mtime >= source_mtime):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        image_source = source if source is not None else io.BytesIO(image_storage_service.get_bytes(relative_path))
        with Image.open(image_source) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            image.save(target, format="PNG", optimize=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="failed to create thumbnail") from exc
    return target


def get_thumbnail_response(relative_path: str) -> FileResponse | Response:
    if _r2_mode() or _r2_image(relative_path):
        return get_image_response(relative_path)
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    return FileResponse(ensure_thumbnail(relative_path), headers=headers)


def get_image_download_response(relative_path: str) -> FileResponse | Response:
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if image_storage_service.has_local(relative_path):
        path = _safe_image_path(relative_path)
        headers = {**cors_headers, "Content-Disposition": f'attachment; filename="{path.name}"'}
        return FileResponse(path, filename=path.name, headers=headers)
    rel = _safe_relative_path(relative_path)
    headers = {
        **cors_headers,
        "Content-Disposition": f'attachment; filename="{Path(rel).name}"',
    }
    return Response(
        content=image_storage_service.get_bytes(rel),
        media_type="image/png",
        headers=headers,
    )


def cleanup_image_thumbnails() -> int:
    if _r2_mode():
        return 0
    thumbnails_root = DATA_DIR / "image_thumbnails"
    removed = 0
    for path in thumbnails_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(thumbnails_root).as_posix()
        if not rel.endswith(".png") or not image_storage_service.exists(rel[:-4]):
            path.unlink()
            removed += 1
    _cleanup_empty_dirs(thumbnails_root)
    return removed


def list_images(base_url: str, start_date: str = "", end_date: str = "") -> dict[str, object]:
    r2_mode = _r2_mode()
    if not r2_mode:
        config.cleanup_old_images()
        cleanup_image_thumbnails()
    all_tags = load_tags()
    items = []
    for item in image_storage_service.list_items(base_url, start_date, end_date):
        r2 = item.get("storage") == "r2"
        # An unavailable/expired R2 reference must not fall back to an app proxy URL.
        url = str(item.get("url") or ("" if r2 else f"{base_url.rstrip('/')}/images/{item['path']}"))
        items.append({
            **item,
            "url": url,
            "thumbnail_url": url if r2 or r2_mode else thumbnail_url(base_url, str(item["path"])),
            "tags": all_tags.get(str(item["path"]), []),
        })
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        groups.setdefault(str(item["date"]), []).append(item)
    return {"items": items, "groups": [{"date": key, "items": value} for key, value in groups.items()]}


def delete_images(paths: list[str] | None = None, start_date: str = "", end_date: str = "", all_matching: bool = False) -> dict[str, int]:
    root = (DATA_DIR / "images").resolve()
    thumbnails_root = DATA_DIR / "image_thumbnails"
    targets = [
        str(item["path"])
        for item in image_storage_service.list_items("", start_date=start_date, end_date=end_date)
    ] if all_matching else (paths or [])
    removed = 0
    local_cleanup = False
    for item in targets:
        rel = _safe_relative_path(item)
        r2 = _r2_image(rel)
        if not r2:
            try:
                (root / rel).resolve().relative_to(root)
            except ValueError:
                continue
        if image_storage_service.delete(rel):
            removed += 1
        if not r2:
            local_cleanup = True
            for thumbnail in (_thumbnail_path(rel), thumbnails_root / rel):
                if thumbnail.is_file():
                    thumbnail.unlink()
        remove_tags(rel)
    if local_cleanup:
        _cleanup_empty_dirs(root)
        _cleanup_empty_dirs(thumbnails_root)
    return {"removed": removed}


def legacy_storage_stats() -> dict[str, int]:
    """Read-only pending cleanup counts, without importing/initializing the task service."""
    thumbnails = sum(path.is_file() for path in (DATA_DIR / "image_thumbnails").rglob("*"))
    payloads = 0
    task_path = DATA_DIR / "image_tasks.json"
    if task_path.is_file():
        raw = json.loads(task_path.read_text(encoding="utf-8"))
        tasks = raw.get("tasks", []) if isinstance(raw, dict) else raw
        for task in tasks:
            data = task.get("data") if isinstance(task, dict) else None
            if not isinstance(data, list):
                continue
            payloads += sum(
                isinstance(item, dict) and (
                    "b64_json" in item or str(item.get("url") or "").strip().lower().startswith("data:")
                )
                for item in data
            )
    return {"legacy_thumbnail_count": thumbnails, "legacy_payload_count": payloads}


def storage_stats() -> dict:
    # Reading config.images_dir would create it, including on an R2-only server.
    usage = shutil.disk_usage(DATA_DIR)
    image_count = image_size = 0
    extra = {}
    if _r2_mode():
        items = image_storage_service.list_items("")
        current = [item for item in items if item.get("storage") == "r2"]
        legacy = [item for item in items if item.get("local")]
        image_count = len(current)
        image_size = sum(int(item.get("size") or 0) for item in current)
        extra = {
            "storage": "r2",
            "legacy_image_count": len(legacy),
            "legacy_size_bytes": sum(int(item.get("size") or 0) for item in legacy),
            **legacy_storage_stats(),
        }
    else:
        for path in (DATA_DIR / "images").rglob("*"):
            if path.is_file():
                image_count += 1
                image_size += path.stat().st_size
    return {
        "disk_total_mb": usage.total // (1024 * 1024),
        "disk_used_mb": usage.used // (1024 * 1024),
        "disk_free_mb": usage.free // (1024 * 1024),
        "image_count": image_count,
        "image_size_mb": image_size // (1024 * 1024),
        "image_size_bytes": image_size,
        **extra,
    }


def compress_images(quality: int = 60) -> dict:
    """重新压缩所有图片，返回节省的空间"""
    if _r2_mode():
        raise HTTPException(status_code=409, detail="local image compression is disabled for R2")
    saved = 0
    count = 0
    for p in sorted((DATA_DIR / "images").rglob("*.png")):
        if not p.is_file():
            continue
        try:
            orig = p.stat().st_size
            with Image.open(p) as img:
                img = ImageOps.exif_transpose(img)
                img.save(str(p) + ".tmp", format="PNG", optimize=True)
            new_size = Path(str(p) + ".tmp").stat().st_size
            if new_size < orig:
                Path(str(p) + ".tmp").replace(p)
                saved += orig - new_size
                count += 1
            else:
                Path(str(p) + ".tmp").unlink()
        except Exception:
            pass
    return {"compressed": count, "saved_bytes": saved, "saved_mb": saved // (1024 * 1024)}


def delete_to_target(target_free_mb: int, dry_run: bool = False) -> dict:
    """删除最旧的图片直到剩余空间达到 target_free_mb"""
    root = DATA_DIR / "images"
    thumbnails_root = DATA_DIR / "image_thumbnails"
    usage = shutil.disk_usage(DATA_DIR)
    current_free = usage.free // (1024 * 1024)
    if current_free >= target_free_mb and not dry_run:
        return {"removed": 0, "current_free_mb": current_free, "target_free_mb": target_free_mb, "done": True}

    files = sorted(
        (p for p in root.rglob("*.png") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    removed = 0
    freed = 0
    for p in files:
        if current_free + freed // (1024 * 1024) >= target_free_mb:
            break
        size = p.stat().st_size
        if not dry_run:
            rel = p.relative_to(root).as_posix()
            for tp in (_thumbnail_path(rel), thumbnails_root / rel):
                if tp.is_file():
                    tp.unlink()
            remove_tags(rel)
            p.unlink()
        freed += size
        removed += 1

    if not dry_run:
        _cleanup_empty_dirs(root)
        _cleanup_empty_dirs(thumbnails_root)

    return {
        "removed": removed,
        "freed_mb": freed // (1024 * 1024),
        "target_free_mb": target_free_mb,
        "current_free_mb": current_free + (freed // (1024 * 1024)),
        "done": (current_free + freed // (1024 * 1024)) >= target_free_mb,
        "dry_run": dry_run,
    }


def cleanup_legacy_images() -> dict[str, int]:
    """Explicitly remove local legacy images/payloads, never R2 or WebDAV objects."""
    from services.image_task_service import image_task_service

    root = DATA_DIR / "images"
    thumbnails_root = DATA_DIR / "image_thumbnails"
    removed = thumbnails = 0
    indexed = image_storage_service._load_index()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        metadata = indexed.get(rel, {})
        if metadata.get("storage") == "r2":
            continue
        # The snapshot selects local files only; never invoke a remote delete or
        # rewrite the index per file. Reconcile once after filesystem changes.
        path.unlink()
        if not metadata.get("webdav") and metadata.get("storage") not in {"webdav", "both"}:
            remove_tags(rel)
        removed += 1
    for path in thumbnails_root.rglob("*"):
        if path.is_file():
            path.unlink()
            thumbnails += 1
    _cleanup_empty_dirs(root)
    _cleanup_empty_dirs(thumbnails_root)
    # Reconcile missing local entries / both->webdav via the storage service's lock.
    image_storage_service.list_items("")
    payloads = image_task_service.clear_legacy_image_payloads()
    return {"removed": removed, "thumbnails": thumbnails, **payloads}


def download_images_zip(paths: list[str]) -> io.BytesIO:
    buf = io.BytesIO()
    added = 0
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in paths:
            rel = _safe_relative_path(item)
            try:
                payload = image_storage_service.get_bytes(rel)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            path = Path(rel)
            name = path.name
            if name in used_names:
                stem = path.stem
                suffix = path.suffix
                counter = 2
                while f"{stem}_{counter}{suffix}" in used_names:
                    counter += 1
                name = f"{stem}_{counter}{suffix}"
            used_names.add(name)
            zf.writestr(name, payload)
            added += 1
    if added == 0:
        raise HTTPException(status_code=404, detail="no images found")
    buf.seek(0)
    return buf


def _auto_cleanup_worker(stop_event: threading.Event) -> None:
    """One loop, independent R2 expiry cleanup and 30-minute local cleanup deadlines."""
    last_r2 = last_legacy = time.monotonic()
    while True:
        settings = config.get_image_storage_settings()
        now = time.monotonic()
        # Re-read settings at least every minute; only active duties affect the wait.
        wait_seconds = 60.0
        if settings.get("r2_accounts"):
            wait_seconds = min(wait_seconds, last_r2 + int(settings.get("cleanup_interval_seconds", 60)) - now)
        if settings.get("mode") != "r2":
            wait_seconds = min(wait_seconds, last_legacy + 1800 - now)
        if stop_event.wait(max(0.0, wait_seconds)):
            return
        settings = config.get_image_storage_settings()
        now = time.monotonic()
        if settings.get("r2_accounts") and now - last_r2 >= int(settings.get("cleanup_interval_seconds", 60)):
            last_r2 = now
            try:
                image_storage_service.cleanup_expired()
            except Exception:
                # No raw transport exceptions/URLs in the maintenance log.
                logger.warning({"event": "image_maintenance_failed", "mode": settings.get("mode")})
        if settings.get("mode") == "r2" or now - last_legacy < 1800:
            continue
        last_legacy = now
        try:
            config.cleanup_old_images()
            cleanup_image_thumbnails()
            min_free_mb = getattr(config, "image_min_free_mb", None)
            if min_free_mb is None:
                min_free_mb = 500
            usage = shutil.disk_usage(DATA_DIR)
            free_mb = usage.free // (1024 * 1024)
            if free_mb < min_free_mb:
                logger.info({"event": "image_auto_cleanup", "free_mb": free_mb, "min_free_mb": min_free_mb})
                result = delete_to_target(min_free_mb)
                logger.info({"event": "image_auto_cleanup_done", **result})
        except Exception:
            logger.warning({"event": "image_maintenance_failed", "mode": settings.get("mode")})


def start_image_cleanup_scheduler(stop_event: threading.Event) -> threading.Thread:
    thread = threading.Thread(
        target=_auto_cleanup_worker, args=(stop_event,), daemon=True, name="image-cleanup",
    )
    thread.start()
    return thread
