from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test.utils import BASE_URL, run_editable_file_task

PROMPT = "按原图位置拆分海报元素并合成可编辑 PSD，同时输出每个图层素材 zip。"
BASE64_IMAGES: list[str] = []
TIMEOUT_SECS = 600
POLL_INTERVAL_SECS = 5


def main() -> None:
    run_editable_file_task(
        "/v1/psd/generations", PROMPT, BASE64_IMAGES,
        base_url=BASE_URL, timeout_secs=TIMEOUT_SECS, poll_interval_secs=POLL_INTERVAL_SECS,
    )


if __name__ == "__main__":
    main()
