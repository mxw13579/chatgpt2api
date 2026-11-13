"""Run selected offline tests against a temporary source copy, never live config/data.

Usage: python test/run_isolated.py test/test_account_incremental_service.py [...]
Requires pytest in the current Python environment. Text tokenization is stubbed
because tiktoken may download assets; these tests do not verify text token counts.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run_worker(arguments: list[str]) -> int:
    import socket
    from unittest.mock import patch
    from contextlib import ExitStack

    sys.path.insert(0, str(ROOT))
    import requests
    from curl_cffi import requests as curl_requests
    import httpx
    import pytest
    import tiktoken

    attempts = []

    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("External network forbidden in offline tests")

    def socket_connect(original):
        def guarded(sock, address):
            # Windows implements stdlib socketpair with a local TCP connection.
            caller = sys._getframe(1)
            if caller.f_globals.get("__name__") == "socket" and caller.f_code.co_name in {"socketpair", "_fallback_socketpair"}:
                return original(sock, address)
            return deny(sock, address)
        return guarded

    class Encoder:
        def encode(self, value, *args, **kwargs):
            return list(str(value).encode("utf-8"))

    with ExitStack() as patches:
        for target, name, value in (
            (socket.socket, "connect", socket_connect(socket.socket.connect)),
            (socket.socket, "connect_ex", socket_connect(socket.socket.connect_ex)),
            (socket, "create_connection", deny),
            (requests.sessions.Session, "request", deny),
            (curl_requests.Session, "request", deny),
            (curl_requests.AsyncSession, "request", deny),
            (httpx.HTTPTransport, "handle_request", deny),
            (httpx.AsyncHTTPTransport, "handle_async_request", deny),
            (tiktoken, "encoding_for_model", lambda *args, **kwargs: Encoder()),
            (tiktoken, "get_encoding", lambda *args, **kwargs: Encoder()),
        ):
            patches.enter_context(patch.object(target, name, value))
        code = pytest.main(["-q", "--tb=short", "-p", "no:cacheprovider", *arguments])
    print(f"Blocked network attempts: {len(attempts)}", flush=True)
    return int(code) or int(bool(attempts))


def main(arguments: list[str]) -> int:
    if not arguments:
        print(__doc__)
        return 2
    with tempfile.TemporaryDirectory(prefix="chatgpt2api-tests-") as directory:
        target = Path(directory)
        for name in ("api", "services", "utils", "test"):
            for source in (ROOT / name).rglob("*.py"):
                destination = target / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        shutil.copy2(ROOT / "VERSION", target / "VERSION")
        (target / "config.json").write_text("{}\n", encoding="utf-8")
        environment = dict(os.environ, STORAGE_BACKEND="json", CHATGPT2API_AUTH_KEY="chatgpt2api", PYTHONDONTWRITEBYTECODE="1")
        for name in ("DATABASE_URL", "GIT_REPO_URL", "GIT_TOKEN"):
            environment.pop(name, None)
        return subprocess.run(
            [sys.executable, str(target / "test" / "run_isolated.py"), "--worker", *arguments],
            cwd=target, env=environment,
        ).returncode


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        raise SystemExit(run_worker(sys.argv[2:]))
    raise SystemExit(main(sys.argv[1:]))
