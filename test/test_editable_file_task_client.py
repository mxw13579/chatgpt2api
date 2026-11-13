"""仅用合成响应验证手动测试入口，不请求真实 PPT/PSD 服务。"""
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test import test_gpt_ppt, test_gpt_psd, utils


@pytest.mark.parametrize("entry,endpoint", [
    (test_gpt_ppt, "/v1/ppt/generations"), (test_gpt_psd, "/v1/psd/generations"),
])
@pytest.mark.parametrize("status", ["success", "error"])
def test_manual_entries_share_submission_and_polling(monkeypatch, entry, endpoint, status):
    monkeypatch.setattr(utils, "load_auth_key", lambda: "synthetic-key")
    monkeypatch.setattr(utils, "uuid4", lambda: "synthetic-client-id")
    clock = SimpleNamespace(time=lambda: 0, sleep=Mock())
    monkeypatch.setattr(utils, "time", clock)
    request = Mock(side_effect=[
        BytesIO(b'{"taskId":"synthetic-task"}'),
        BytesIO(json.dumps({"items": [{"status": status}]}).encode()),
    ])
    monkeypatch.setattr(utils.urllib.request, "urlopen", request)
    entry.main()
    post, poll = [call.args[0] for call in request.call_args_list]
    assert post.full_url == entry.BASE_URL + endpoint
    assert post.method == "POST"
    assert json.loads(post.data) == {
        "client_task_id": "synthetic-client-id", "prompt": entry.PROMPT, "base64_images": entry.BASE64_IMAGES,
    }
    assert poll.full_url == entry.BASE_URL + "/v1/editable-file-tasks?ids=synthetic-task"
    assert poll.method == "GET"
    clock.sleep.assert_called_once_with(entry.POLL_INTERVAL_SECS)


def test_manual_task_timeout_is_preserved(monkeypatch):
    monkeypatch.setattr(utils, "load_auth_key", lambda: "synthetic-key")
    monkeypatch.setattr(utils, "time", SimpleNamespace(time=Mock(side_effect=[0, 601]), sleep=Mock()))
    request = Mock(return_value=BytesIO(b'{"id":"synthetic-task"}'))
    monkeypatch.setattr(utils.urllib.request, "urlopen", request)
    with pytest.raises(TimeoutError, match="synthetic-task"):
        utils.run_editable_file_task("/v1/ppt/generations", "synthetic", [])
    request.assert_called_once()
