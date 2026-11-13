from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test.utils import BASE_URL, run_editable_file_task

PROMPT = "生成一份 2026 年 Q2 电商运营复盘 PPT，8 页以内，商务科技风，包含销售、用户、渠道、广告、618 活动和 Q3 规划。"
BASE64_IMAGES: list[str] = []
TIMEOUT_SECS = 600
POLL_INTERVAL_SECS = 5


def main() -> None:
    run_editable_file_task(
        "/v1/ppt/generations", PROMPT, BASE64_IMAGES,
        base_url=BASE_URL, timeout_secs=TIMEOUT_SECS, poll_interval_secs=POLL_INTERVAL_SECS,
    )


if __name__ == "__main__":
    main()
