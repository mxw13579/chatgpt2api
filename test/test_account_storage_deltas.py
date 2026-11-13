"""Synthetic storage fixtures only; no AccountService, configuration or network I/O."""

import traceback
from copy import deepcopy
from unittest.mock import Mock

import pytest

from services.storage.base import StorageBackend
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.git_storage import GitStorageBackend
from services.storage.json_storage import JSONStorageBackend


_TOKEN = "synthetic-private-access-token"
_PASSWORD = "synthetic-private-password"


@pytest.fixture(params=["json", "database"])
def backend(request, tmp_path):
    if request.param == "json":
        yield JSONStorageBackend(tmp_path / "synthetic-accounts.json")
    else:
        storage = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'synthetic-accounts.db'}")
        try:
            yield storage
        finally:
            storage.engine.dispose()


def _reject_io(*args, **kwargs):
    raise AssertionError("Unexpected storage I/O")


def _assert_redacted(error):
    rendered = "".join(traceback.format_exception(error))
    for marker in (_TOKEN, _PASSWORD):
        assert marker not in str(error)
        assert marker not in repr(error)
        assert marker not in rendered


def test_json_and_git_inherit_non_abstract_delta_fallback():
    assert JSONStorageBackend.apply_account_changes is StorageBackend.apply_account_changes
    assert GitStorageBackend.apply_account_changes is StorageBackend.apply_account_changes
    assert "apply_account_changes" not in StorageBackend.__abstractmethods__


def test_account_deltas_replace_records_preserve_unrelated_and_do_not_mutate_inputs(backend):
    original = [
        {"access_token": "synthetic-a", "name": "A", "obsolete": True},
        {"access_token": "synthetic-b", "name": "B"},
        {"access_token": "synthetic-c", "name": "C", "nested": {"quota": 3}},
    ]
    backend.save_accounts(original)
    upserts = [
        {"access_token": "synthetic-a", "name": "changed", "nested": {"quota": 2}},
        {"access_token": "synthetic-d", "name": "new"},
    ]
    deletions = ["synthetic-a", "synthetic-b", "synthetic-b", "synthetic-missing"]
    inputs_before = deepcopy((original, upserts, deletions))

    backend.apply_account_changes(upserts, deletions)

    loaded = {item["access_token"]: item for item in backend.load_accounts()}
    assert loaded == {
        "synthetic-a": upserts[0],
        "synthetic-c": original[2],
        "synthetic-d": upserts[1],
    }
    assert (original, upserts, deletions) == inputs_before
    # Repeated deletion of an already absent key is harmless.
    backend.apply_account_changes([], ["synthetic-b", "synthetic-b"])
    assert {item["access_token"]: item for item in backend.load_accounts()} == loaded


def test_empty_delta_without_snapshot_performs_no_io(backend, monkeypatch):
    monkeypatch.setattr(backend, "load_accounts", _reject_io)
    monkeypatch.setattr(backend, "save_accounts", _reject_io)
    if isinstance(backend, DatabaseStorageBackend):
        monkeypatch.setattr(backend, "Session", _reject_io)
    backend.apply_account_changes([], [])


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"upserts": None}, id="null-upserts"),
        pytest.param({"upserts": ()}, id="tuple-upserts"),
        pytest.param({"upserts": {"access_token": _TOKEN}}, id="mapping-upserts"),
        pytest.param({"upserts": [None]}, id="non-dictionary-record"),
        pytest.param({"upserts": [{"password": _PASSWORD}]}, id="missing-token"),
        pytest.param({"upserts": [{"access_token": None}]}, id="null-token"),
        pytest.param({"upserts": [{"access_token": 123}]}, id="numeric-token"),
        pytest.param({"upserts": [{"access_token": True}]}, id="boolean-token"),
        pytest.param({"upserts": [{"access_token": b"synthetic-bytes"}]}, id="bytes-token"),
        pytest.param({"upserts": [{"access_token": ""}]}, id="empty-token"),
        pytest.param({"upserts": [{"access_token": " \t"}]}, id="blank-token"),
        pytest.param({"upserts": [{"access_token": f" {_TOKEN} "}]}, id="padded-token"),
        pytest.param(
            {"upserts": [{"access_token": _TOKEN, "password": {_PASSWORD}}]},
            id="unserializable-data",
        ),
        pytest.param(
            {"upserts": [{"access_token": _TOKEN, "quota": float("nan")}]},
            id="non-finite-data",
        ),
        pytest.param({"deleted_tokens": None}, id="null-deletions"),
        pytest.param({"deleted_tokens": _TOKEN}, id="string-deletions"),
        pytest.param({"deleted_tokens": (_TOKEN,)}, id="tuple-deletions"),
        pytest.param({"deleted_tokens": [None]}, id="null-deleted-token"),
        pytest.param({"deleted_tokens": [123]}, id="numeric-deleted-token"),
        pytest.param({"deleted_tokens": [True]}, id="boolean-deleted-token"),
        pytest.param({"deleted_tokens": [""]}, id="empty-deleted-token"),
        pytest.param({"deleted_tokens": [" \t"]}, id="blank-deleted-token"),
        pytest.param({"deleted_tokens": [f" {_TOKEN} "]}, id="padded-deleted-token"),
        pytest.param({"snapshot": {"access_token": _TOKEN}}, id="mapping-snapshot"),
        pytest.param({"snapshot": ()}, id="tuple-snapshot"),
    ],
)
def test_invalid_deltas_fail_before_io_without_disclosing_credentials(backend, monkeypatch, arguments):
    original = {"access_token": _TOKEN, "password": _PASSWORD, "name": "original"}
    backend.save_accounts([original])
    kwargs = {
        "upserts": [{"access_token": _TOKEN, "password": _PASSWORD, "name": "changed"}],
        "deleted_tokens": ["synthetic-delete"],
        "snapshot": [original],
    }
    kwargs.update(arguments)

    with monkeypatch.context() as guarded:
        guarded.setattr(backend, "load_accounts", _reject_io)
        guarded.setattr(backend, "save_accounts", _reject_io)
        if isinstance(backend, DatabaseStorageBackend):
            guarded.setattr(backend, "Session", _reject_io)
        with pytest.raises(ValueError) as exc_info:
            backend.apply_account_changes(**kwargs)

    _assert_redacted(exc_info.value)
    assert backend.load_accounts() == [original]


@pytest.mark.parametrize("existing", [False, True], ids=["new-key", "existing-key"])
@pytest.mark.parametrize("identical", [False, True], ids=["conflicting-records", "identical-records"])
def test_duplicate_upserts_are_rejected_atomically(backend, monkeypatch, existing, identical):
    original = {"access_token": "synthetic-keep", "name": "original"}
    initial = [original]
    if existing:
        initial.append({"access_token": _TOKEN, "name": "old"})
    backend.save_accounts(initial)
    first = {"access_token": _TOKEN, "name": "first", "password": _PASSWORD}
    second = dict(first) if identical else {**first, "name": "second"}

    with monkeypatch.context() as guarded:
        guarded.setattr(backend, "load_accounts", _reject_io)
        guarded.setattr(backend, "save_accounts", _reject_io)
        if isinstance(backend, DatabaseStorageBackend):
            guarded.setattr(backend, "Session", _reject_io)
        with pytest.raises(ValueError, match="Duplicate access_token") as exc_info:
            backend.apply_account_changes([first, second], ["synthetic-keep", _TOKEN])

    _assert_redacted(exc_info.value)
    assert {item["access_token"]: item for item in backend.load_accounts()} == {
        item["access_token"]: item for item in initial
    }


def test_all_records_are_serialized_before_any_change(backend):
    original = {"access_token": _TOKEN, "password": _PASSWORD}
    backend.save_accounts([original])
    circular = {"access_token": "synthetic-circular", "password": _PASSWORD}
    circular["self"] = circular

    with pytest.raises(ValueError, match="valid JSON data") as exc_info:
        backend.apply_account_changes(
            [{"access_token": "synthetic-valid-new"}, circular], [_TOKEN],
        )

    _assert_redacted(exc_info.value)
    assert backend.load_accounts() == [original]


def test_json_fallback_without_snapshot_loads_and_saves_once(tmp_path, monkeypatch):
    backend = JSONStorageBackend(tmp_path / "synthetic-accounts.json")
    backend.save_accounts([
        {"access_token": "synthetic-old", "name": "old"},
        {"access_token": "synthetic-keep", "name": "keep"},
    ])
    load = Mock(wraps=backend.load_accounts)
    save = Mock(wraps=backend.save_accounts)
    monkeypatch.setattr(backend, "load_accounts", load)
    monkeypatch.setattr(backend, "save_accounts", save)
    new_account = {"access_token": "synthetic-new", "name": "new"}

    backend.apply_account_changes([new_account], ["synthetic-old"])

    load.assert_called_once_with()
    save.assert_called_once_with([
        {"access_token": "synthetic-keep", "name": "keep"}, new_account,
    ])
    assert JSONStorageBackend.load_accounts(backend) == save.call_args.args[0]


@pytest.mark.parametrize("snapshot", [[], [{"access_token": "synthetic-snapshot", "name": "final"}]])
def test_json_fallback_uses_authoritative_snapshot_even_for_empty_delta(tmp_path, monkeypatch, snapshot):
    backend = JSONStorageBackend(tmp_path / "synthetic-accounts.json")
    backend.save_accounts([{"access_token": "synthetic-stale"}])
    before = deepcopy(snapshot)
    save = Mock(wraps=backend.save_accounts)
    monkeypatch.setattr(backend, "load_accounts", _reject_io)
    monkeypatch.setattr(backend, "save_accounts", save)

    backend.apply_account_changes([], [], snapshot=snapshot)

    save.assert_called_once_with(snapshot)
    assert save.call_args.args[0] is snapshot
    assert JSONStorageBackend.load_accounts(backend) == snapshot
    assert snapshot == before


def test_json_fallback_does_not_reapply_delta_to_supplied_snapshot(tmp_path, monkeypatch):
    backend = JSONStorageBackend(tmp_path / "synthetic-accounts.json")
    final = [{"access_token": "synthetic-final", "name": "authoritative"}]
    monkeypatch.setattr(backend, "load_accounts", _reject_io)

    backend.apply_account_changes(
        [{"access_token": "synthetic-delta-only"}], ["synthetic-final"], snapshot=final,
    )

    assert JSONStorageBackend.load_accounts(backend) == final


def test_json_snapshot_is_encoded_once_when_saved(tmp_path, monkeypatch):
    import json

    backend = JSONStorageBackend(tmp_path / "synthetic-accounts.json")
    snapshot = [{"access_token": _TOKEN, "quota": 3}]
    encode = Mock(wraps=json.dumps)
    monkeypatch.setattr(json, "dumps", encode)
    monkeypatch.setattr(backend, "load_accounts", _reject_io)

    backend.apply_account_changes([], [], snapshot=snapshot)

    encode.assert_called_once_with(snapshot, ensure_ascii=False, indent=2)
    assert JSONStorageBackend.load_accounts(backend) == snapshot


@pytest.mark.parametrize("method", ["load_accounts", "save_accounts"])
def test_fallback_backend_errors_are_redacted(tmp_path, monkeypatch, method):
    backend = JSONStorageBackend(tmp_path / "synthetic-accounts.json")

    def fail(*args, **kwargs):
        raise OSError(f"{_TOKEN} {_PASSWORD}")

    monkeypatch.setattr(backend, method, fail)
    with pytest.raises(RuntimeError, match="^Failed to apply account changes$") as exc_info:
        backend.apply_account_changes([{"access_token": "synthetic-new"}], [])

    _assert_redacted(exc_info.value)
