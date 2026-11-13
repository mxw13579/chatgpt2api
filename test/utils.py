import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


ROOT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT_DIR / "data" / "output"
BASE_URL = "http://127.0.0.1:8000"

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def load_auth_key() -> str:
    return json.loads((ROOT_DIR / "config.json").read_text(encoding="utf-8"))["auth-key"]


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {load_auth_key()}"},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def run_editable_file_task(
    endpoint: str, prompt: str, base64_images: list[str], *,
    base_url: str = BASE_URL, timeout_secs: float = 600, poll_interval_secs: float = 5,
) -> None:
    """PPT/PSD 手动测试共用的提交与轮询流程。"""
    def request_json(method: str, path: str, payload: dict | None = None) -> dict:
        api_key = load_auth_key()
        if not api_key.strip():
            raise ValueError("API_KEY is empty")
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(
            base_url.rstrip("/") + path, data=data, method=method,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", "replace")) from exc

    task = request_json("POST", endpoint, {
        "client_task_id": str(uuid4()), "prompt": prompt, "base64_images": base64_images,
    })
    task_id = str(task.get("taskId") or task.get("id") or "")
    if not task_id:
        raise RuntimeError(f"missing taskId: {task}")
    print(json.dumps(task, ensure_ascii=False, indent=2))
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        time.sleep(poll_interval_secs)
        status = request_json("GET", "/v1/editable-file-tasks?ids=" + urllib.parse.quote(task_id))
        print(json.dumps(status, ensure_ascii=False, indent=2))
        item = (status.get("items") or [{}])[0]
        if item.get("status") in {"success", "error"}:
            return
    raise TimeoutError(f"task timeout: {task_id}")


def detect_ext(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".png"


def save_image(image_b64: str, name: str) -> Path:
    image_bytes = base64.b64decode(image_b64)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}_{int(time.time())}{detect_ext(image_bytes)}"
    path.write_bytes(image_bytes)
    return path
