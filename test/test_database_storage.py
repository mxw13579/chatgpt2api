import json
import traceback
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from services.storage.database_storage import (
    AccountModel,
    AuthKeyModel,
    DatabaseStorageBackend,
)


def _account_rows(backend: DatabaseStorageBackend) -> dict[str, AccountModel]:
    session = backend.Session()
    try:
        return {
            row.access_token: row
            for row in session.query(AccountModel).order_by(AccountModel.id).all()
        }
    finally:
        session.close()


def test_save_accounts_accepts_access_token_longer_than_legacy_varchar_limit(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    token = "eyJ" + ("a" * 4000)
    backend.save_accounts([{"access_token": token, "name": "long"}])

    loaded = backend.load_accounts()
    assert loaded == [{"access_token": token, "name": "long"}]
    assert len(loaded[0]["access_token"]) > 2048


def test_save_accounts_preserves_existing_rows_and_updates_only_changed_data(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    backend.save_accounts(
        [
            {"access_token": "token-a", "name": "A"},
            {"access_token": "token-b", "name": "B"},
        ]
    )

    before = _account_rows(backend)
    before_ids = {token: row.id for token, row in before.items()}
    before_b_data = before["token-b"].data

    backend.save_accounts(
        [
            {"access_token": "token-b", "name": "B"},
            {"access_token": "token-a", "name": "A updated"},
            {"access_token": "token-c", "name": "C"},
        ]
    )

    after = _account_rows(backend)
    assert set(after) == {"token-a", "token-b", "token-c"}
    assert after["token-a"].id == before_ids["token-a"]
    assert after["token-b"].id == before_ids["token-b"]
    assert after["token-b"].data == before_b_data
    assert json.loads(after["token-a"].data)["name"] == "A updated"


def test_save_accounts_deletes_only_rows_missing_from_new_snapshot(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    backend.save_accounts(
        [
            {"access_token": "token-a", "name": "A"},
            {"access_token": "token-b", "name": "B"},
        ]
    )
    before = _account_rows(backend)
    token_b_id = before["token-b"].id

    backend.save_accounts([{"access_token": "token-b", "name": "B"}])

    after = _account_rows(backend)
    assert set(after) == {"token-b"}
    assert after["token-b"].id == token_b_id


def test_save_accounts_rejects_duplicate_tokens_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    original = {"access_token": "token-a", "name": "A"}
    backend.save_accounts([original])

    with pytest.raises(ValueError, match="Duplicate access_token") as exc_info:
        backend.save_accounts(
            [
                {"access_token": "token-a", "name": "first update"},
                {"access_token": "token-a", "name": "second update"},
            ]
        )

    assert "token-a" not in str(exc_info.value)
    assert backend.load_accounts() == [original]


def test_save_accounts_rejects_duplicate_new_tokens_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'accounts.db'}")
    original = {"access_token": "token-a", "name": "A"}
    backend.save_accounts([original])

    with pytest.raises(ValueError, match="Duplicate access_token") as exc_info:
        backend.save_accounts(
            [
                original,
                {"access_token": "token-b", "name": "first new"},
                {"access_token": "token-b", "name": "second new"},
            ]
        )

    assert "token-b" not in str(exc_info.value)
    assert backend.load_accounts() == [original]


def test_save_auth_keys_preserves_ids_with_target_key_mapping(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'auth-keys.db'}")
    backend.save_auth_keys(
        [
            {"id": "key-a", "name": "A"},
            {"id": "key-b", "name": "B"},
        ]
    )

    session = backend.Session()
    try:
        before_ids = {
            row.key_id: row.id
            for row in session.query(AuthKeyModel).order_by(AuthKeyModel.id).all()
        }
    finally:
        session.close()

    backend.save_auth_keys(
        [
            {"id": "key-b", "name": "B"},
            {"id": "key-a", "name": "A updated"},
            {"id": "key-c", "name": "C"},
        ]
    )

    session = backend.Session()
    try:
        after = {
            row.key_id: row
            for row in session.query(AuthKeyModel).order_by(AuthKeyModel.id).all()
        }
        assert set(after) == {"key-a", "key-b", "key-c"}
        assert after["key-a"].id == before_ids["key-a"]
        assert after["key-b"].id == before_ids["key-b"]
        assert json.loads(after["key-a"].data)["name"] == "A updated"
    finally:
        session.close()


def test_save_auth_keys_rejects_duplicate_ids_and_rolls_back(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'auth-keys.db'}")
    original = {"id": "key-a", "name": "A"}
    backend.save_auth_keys([original])

    with pytest.raises(ValueError, match="Duplicate id") as exc_info:
        backend.save_auth_keys(
            [
                {"id": "key-a", "name": "first update"},
                {"id": "key-a", "name": "second update"},
            ]
        )

    assert "key-a" not in str(exc_info.value)
    assert backend.load_auth_keys() == [original]


@pytest.fixture
def delta_backend(tmp_path):
    backend = DatabaseStorageBackend(f"sqlite:///{tmp_path / 'synthetic-deltas.db'}")
    try:
        yield backend
    finally:
        backend.engine.dispose()


@contextmanager
def _capture_account_sql(backend):
    """Capture SQL shape and bind counts only, never credential values."""
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.upper().split())
        if "ACCOUNTS" in normalized:
            if executemany and parameters and isinstance(parameters[0], (tuple, list, dict)):
                bind_count = len(parameters[0])
            else:
                # insertmanyvalues may have executemany=True with flat parameters.
                bind_count = len(parameters)
            statements.append((normalized, bind_count))

    event.listen(backend.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(backend.engine, "before_cursor_execute", capture)


def _account_state(backend):
    return {token: (row.id, row.data) for token, row in _account_rows(backend).items()}


def _reject_snapshot_io(*args, **kwargs):
    raise AssertionError("Delta persistence must not use snapshot I/O")


def test_apply_account_changes_single_update_only_selects_changed_key(delta_backend, monkeypatch):
    backend = delta_backend
    backend.save_accounts([
        {"access_token": "synthetic-a", "name": "A"},
        {"access_token": "synthetic-b", "name": "B"},
    ])
    before = _account_state(backend)
    monkeypatch.setattr(backend, "load_accounts", _reject_snapshot_io)
    monkeypatch.setattr(backend, "save_accounts", _reject_snapshot_io)

    with _capture_account_sql(backend) as statements:
        backend.apply_account_changes(
            [{"access_token": "synthetic-a", "name": "已更新"}],
            [],
            snapshot=[{"access_token": "synthetic-snapshot-only"}],
        )

    selects = [(sql, count) for sql, count in statements if sql.startswith("SELECT ")]
    assert len(selects) == 1
    assert " WHERE ACCOUNTS.ACCESS_TOKEN IN (" in selects[0][0]
    assert selects[0][1] == 1
    updates = [sql for sql, _ in statements if sql.startswith("UPDATE ")]
    assert len(updates) == 1
    assert " WHERE " in updates[0]
    assert not any(sql.startswith(("INSERT ", "DELETE ")) for sql, _ in statements)
    after = _account_state(backend)
    assert set(after) == set(before)
    assert after["synthetic-b"] == before["synthetic-b"]
    assert after["synthetic-a"][0] == before["synthetic-a"][0]
    assert json.loads(after["synthetic-a"][1])["name"] == "已更新"


def test_apply_account_changes_insert_does_not_remove_existing_rows(delta_backend):
    backend = delta_backend
    backend.save_accounts([{"access_token": "synthetic-existing", "name": "keep"}])
    before = _account_state(backend)
    new_account = {"access_token": "synthetic-new-" + "x" * 4000, "name": "new"}

    with _capture_account_sql(backend) as statements:
        backend.apply_account_changes([new_account], [])

    after = _account_state(backend)
    assert after["synthetic-existing"] == before["synthetic-existing"]
    assert json.loads(after[new_account["access_token"]][1]) == new_account
    assert len(after) == 2
    assert all(" WHERE " in sql for sql, _ in statements if sql.startswith("SELECT "))
    assert not any(sql.startswith("DELETE ") for sql, _ in statements)


def test_apply_account_changes_delete_only_has_no_select(delta_backend):
    backend = delta_backend
    backend.save_accounts([
        {"access_token": "synthetic-delete"},
        {"access_token": "synthetic-keep"},
    ])
    before = _account_state(backend)

    with _capture_account_sql(backend) as statements:
        backend.apply_account_changes([], ["synthetic-delete", "synthetic-delete", "synthetic-missing"])

    assert _account_state(backend) == {"synthetic-keep": before["synthetic-keep"]}
    assert len(statements) == 1
    assert statements[0][0].startswith("DELETE FROM ACCOUNTS WHERE ")
    assert statements[0][1] == 2


def test_apply_account_changes_upsert_wins_without_replacing_row(delta_backend):
    backend = delta_backend
    original = {"access_token": "synthetic-a", "name": "A"}
    backend.save_accounts([original, {"access_token": "synthetic-keep"}])
    before = _account_state(backend)

    with _capture_account_sql(backend) as statements:
        backend.apply_account_changes([original], ["synthetic-a", "synthetic-a"])

    assert _account_state(backend) == before
    assert statements
    assert all(sql.startswith("SELECT ") and " WHERE " in sql for sql, _ in statements)

    backend.apply_account_changes(
        [{"access_token": "synthetic-a", "name": "changed"}], ["synthetic-a"],
    )
    after = _account_state(backend)
    assert after["synthetic-a"][0] == before["synthetic-a"][0]
    assert json.loads(after["synthetic-a"][1])["name"] == "changed"
    assert after["synthetic-keep"] == before["synthetic-keep"]


def test_apply_account_changes_token_rotation_commits_once(delta_backend):
    backend = delta_backend
    backend.save_accounts([
        {"access_token": "synthetic-old", "refresh_token": "synthetic-refresh"},
        {"access_token": "synthetic-keep"},
    ])
    before = _account_state(backend)
    commits = []

    def record_commit(conn):
        commits.append(True)

    event.listen(backend.engine, "commit", record_commit)
    try:
        backend.apply_account_changes(
            [{"access_token": "synthetic-new", "refresh_token": "synthetic-new-refresh"}],
            ["synthetic-old"],
        )
    finally:
        event.remove(backend.engine, "commit", record_commit)

    assert commits == [True]
    after = _account_state(backend)
    assert set(after) == {"synthetic-new", "synthetic-keep"}
    assert after["synthetic-keep"] == before["synthetic-keep"]
    assert json.loads(after["synthetic-new"][1])["refresh_token"] == "synthetic-new-refresh"


@pytest.mark.parametrize("snapshot", [None, [], [{"access_token": "synthetic-ignored"}]])
def test_apply_account_changes_empty_delta_never_opens_session(delta_backend, monkeypatch, snapshot):
    monkeypatch.setattr(delta_backend, "Session", _reject_snapshot_io)
    monkeypatch.setattr(delta_backend, "load_accounts", _reject_snapshot_io)
    monkeypatch.setattr(delta_backend, "save_accounts", _reject_snapshot_io)
    delta_backend.apply_account_changes([], [], snapshot=snapshot)


def test_apply_account_changes_does_not_iterate_snapshot(delta_backend):
    class UnreadSnapshot(list):
        def __iter__(self):
            raise AssertionError("Database deltas must not inspect the full snapshot")

    delta_backend.apply_account_changes(
        [{"access_token": "synthetic-a"}], [], snapshot=UnreadSnapshot(),
    )
    assert delta_backend.load_accounts() == [{"access_token": "synthetic-a"}]


@pytest.mark.parametrize(
    ("upserts", "deletions", "expected_operations"),
    [
        pytest.param([{"access_token": "synthetic-new"}], [], {"INSERT"}, id="insert"),
        pytest.param([], ["synthetic-old"], {"DELETE"}, id="delete"),
        pytest.param([{"access_token": "synthetic-old", "name": "changed"}], [], {"UPDATE"}, id="update"),
        pytest.param(
            [{"access_token": "synthetic-new"}], ["synthetic-old"],
            {"INSERT", "DELETE"}, id="token-rotation",
        ),
    ],
)
def test_apply_account_changes_rolls_back_dml_on_commit_failure(
    delta_backend, upserts, deletions, expected_operations,
):
    backend = delta_backend
    backend.save_accounts([
        {"access_token": "synthetic-old", "name": "original"},
        {"access_token": "synthetic-keep"},
    ])
    before = _account_state(backend)
    marker = "synthetic-sensitive-error-payload"
    rollbacks = []

    def fail_commit(session):
        raise IntegrityError("synthetic failure", {"credential": marker}, RuntimeError(marker))

    def record_rollback(conn):
        rollbacks.append(True)

    event.listen(backend.Session, "before_commit", fail_commit)
    event.listen(backend.engine, "rollback", record_rollback)
    try:
        with _capture_account_sql(backend) as statements:
            with pytest.raises(RuntimeError, match="^Failed to apply account changes$") as exc_info:
                backend.apply_account_changes(upserts, deletions)
    finally:
        event.remove(backend.Session, "before_commit", fail_commit)
        event.remove(backend.engine, "rollback", record_rollback)

    # DML really ran before the injected commit failure; this is not just validation failure.
    assert expected_operations <= {sql.split()[0] for sql, _ in statements}
    assert rollbacks == [True]
    assert _account_state(backend) == before
    assert marker not in str(exc_info.value)
    assert marker not in "".join(traceback.format_exception(exc_info.value))
    # A failed transaction must not leave the backend unusable.
    backend.apply_account_changes(upserts, deletions)


def test_apply_account_changes_rotation_rolls_back_when_insert_flush_fails(delta_backend):
    backend = delta_backend
    backend.save_accounts([
        {"access_token": "synthetic-old"},
        {"access_token": "synthetic-keep"},
    ])
    before = _account_state(backend)
    new_account = {
        "access_token": "synthetic-sensitive-new-token",
        "password": "synthetic-sensitive-password",
    }

    def violate_not_null(mapper, connection, target):
        target.data = None  # Exercise a real SQLite constraint failure during flush.

    event.listen(AccountModel, "before_insert", violate_not_null)
    try:
        with _capture_account_sql(backend) as statements:
            with pytest.raises(RuntimeError, match="^Failed to apply account changes$") as exc_info:
                backend.apply_account_changes([new_account], ["synthetic-old"])
    finally:
        event.remove(AccountModel, "before_insert", violate_not_null)

    assert {"DELETE", "INSERT"} <= {sql.split()[0] for sql, _ in statements}
    assert _account_state(backend) == before
    rendered = "".join(traceback.format_exception(exc_info.value))
    for marker in new_account.values():
        assert marker not in str(exc_info.value)
        assert marker not in rendered


def test_apply_account_changes_rolls_back_earlier_chunks(delta_backend, monkeypatch):
    backend = delta_backend
    monkeypatch.setattr(backend, "_ACCOUNT_CHANGE_BATCH_SIZE", 2)
    backend.save_accounts([
        {"access_token": "synthetic-update", "name": "original"},
        *[{"access_token": f"synthetic-delete-{i}"} for i in range(3)],
        {"access_token": "synthetic-keep"},
    ])
    before = _account_state(backend)
    selects = []

    def fail_second_select(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT "):
            selects.append(True)
            if len(selects) == 2:
                raise RuntimeError("synthetic later batch failure")

    event.listen(backend.engine, "before_cursor_execute", fail_second_select)
    try:
        with _capture_account_sql(backend) as statements:
            with pytest.raises(RuntimeError, match="^Failed to apply account changes$"):
                backend.apply_account_changes(
                    [
                        {"access_token": "synthetic-update", "name": "changed"},
                        *[{"access_token": f"synthetic-new-{i}"} for i in range(3)],
                    ],
                    [f"synthetic-delete-{i}" for i in range(3)],
                )
    finally:
        event.remove(backend.engine, "before_cursor_execute", fail_second_select)

    assert len(selects) == 2
    assert {"DELETE", "UPDATE", "INSERT"} <= {sql.split()[0] for sql, _ in statements}
    assert _account_state(backend) == before


def test_apply_account_changes_chunks_large_batches(delta_backend):
    backend = delta_backend
    count = 1005  # Greater than the legacy SQLite parameter limit.
    backend.save_accounts([
        *[{"access_token": f"synthetic-old-{i}"} for i in range(count)],
        {"access_token": "synthetic-keep", "name": "unchanged"},
    ])
    keep = _account_state(backend)["synthetic-keep"]
    upserts = [{"access_token": f"synthetic-new-{i}", "name": "new"} for i in range(count)]

    with _capture_account_sql(backend) as statements:
        backend.apply_account_changes(upserts, [f"synthetic-old-{i}" for i in range(count)])

    batch_size = backend._ACCOUNT_CHANGE_BATCH_SIZE
    expected_batches = (count + batch_size - 1) // batch_size
    for operation in ("SELECT ", "DELETE "):
        batches = [(sql, size) for sql, size in statements if sql.startswith(operation)]
        assert len(batches) == expected_batches
        assert sum(size for _, size in batches) == count
        assert all(" WHERE " in sql and 0 < size <= batch_size for sql, size in batches)
    assert all(size <= 999 for _, size in statements)
    after = _account_state(backend)
    assert set(after) == {item["access_token"] for item in upserts} | {"synthetic-keep"}
    assert after["synthetic-keep"] == keep


def test_save_accounts_empty_snapshot_retains_full_import_semantics(delta_backend):
    delta_backend.apply_account_changes([{"access_token": "synthetic-a"}], [])
    delta_backend.save_accounts([])
    assert delta_backend.load_accounts() == []
