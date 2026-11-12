"""Pure in-memory fixtures; never import the application or construct its singleton.

The coordinator must run these in its approved isolated test environment. Synthetic
account changes do not exercise or establish real authentication/purchase behavior.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from weakref import WeakValueDictionary


class SnapshotStorageContract:
    """Synthetic implementation of the frozen base/JSON/Git fallback contract."""

    def apply_account_changes(self, upserts, deleted_tokens, *, snapshot=None):
        if snapshot is None:
            raise AssertionError("snapshot fallback requires a snapshot")
        self.save_accounts(snapshot)


class LegacyMemoryStorage:
    def __init__(self, accounts=()):
        self.rows = {item["access_token"]: deepcopy(item) for item in accounts}
        self.save_calls = []
        self.fail = False

    def load_accounts(self):
        return deepcopy(list(self.rows.values()))

    def save_accounts(self, accounts):
        self.save_calls.append(deepcopy(accounts))
        if self.fail:
            raise OSError("synthetic-storage-secret")
        self.rows = {item["access_token"]: deepcopy(item) for item in accounts}


class SnapshotMemoryStorage(LegacyMemoryStorage, SnapshotStorageContract):
    pass


class MemoryIncrementalStorage(LegacyMemoryStorage):
    def __init__(self, accounts=()):
        super().__init__(accounts)
        self.change_calls = []
        self.lock_probe = lambda: None

    def apply_account_changes(self, upserts, deleted_tokens, *, snapshot=None):
        self.lock_probe()
        self.change_calls.append((deepcopy(upserts), list(deleted_tokens), snapshot))
        if self.fail:
            raise OSError("synthetic-storage-secret")
        for token in deleted_tokens:
            self.rows.pop(token, None)
        for item in upserts:
            self.rows[item["access_token"]] = deepcopy(item)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now


def load_isolated_service(storage=None):
    """Load only approved source definitions, with fail-closed dependency stubs.

    No services package, production config, storage driver, logging, credential
    directory, singleton initialization, HTTP library, or real authentication is
    imported. Even accidental dynamic project/network imports fail closed.
    """
    path = Path(__file__).resolve().parents[1] / "services" / "account_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    safe_imports = {
        "__future__", "base64", "json", "secrets", "time", "uuid",
        "concurrent.futures", "copy", "datetime", "pathlib", "threading",
        "typing", "urllib.parse",
    }
    definitions = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            definitions.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module in safe_imports:
            definitions.append(node)
        elif isinstance(node, ast.Import) and all(alias.name in safe_imports for alias in node.names):
            definitions.append(node)
    dependencies = {}

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in dependencies:
            return dependencies[name]
        if name not in safe_imports:
            raise AssertionError("non-fixture import blocked")
        return builtins.__import__(name, globals, locals, fromlist, level)

    config = SimpleNamespace(
        image_account_concurrency=4,
        auto_remove_invalid_accounts=False,
        auto_remove_rate_limited_accounts=False,
        auto_relogin_after_refresh=False,
    )
    namespace = {
        "__name__": "isolated_account_service_fixture",
        "__builtins__": {**vars(builtins), "__import__": guarded_import},
        "config": config,
        "StorageBackend": SnapshotStorageContract,
        "LOG_TYPE_ACCOUNT": "account",
        "log_service": SimpleNamespace(add=Mock()),
        # The source's sys import is deliberately excluded; diagnostics stay in memory.
        "sys": SimpleNamespace(stderr=SimpleNamespace(write=Mock())),
        "anonymize_token": lambda token: "token:" + hashlib.sha256(str(token).encode()).hexdigest()[:16],
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    cls = namespace["AccountService"]
    cls._load_cumulative_total = lambda self: len(self._accounts)
    cls._save_cumulative_total = lambda self: None

    def forbidden_auth(*args, **kwargs):
        raise AssertionError("real authentication forbidden in fixture")

    cls._login_with_password = forbidden_auth
    cls._request_access_token_refresh = forbidden_auth
    clock = Clock()
    namespace["time"] = clock
    service = cls(storage if storage is not None else MemoryIncrementalStorage())
    return SimpleNamespace(
        service=service, namespace=namespace, config=config, clock=clock,
        dependencies=dependencies,
        persistence_error=namespace["AccountPersistenceError"],
        capacity_error=namespace["ProgressCapacityError"],
    )


def install_log_io_failure(fixture, operation):
    """Exercise the real add/rotation definitions with an entirely fake filesystem.

    Never import services.log_service or call its constructor/singleton. Only
    synthetic bytes are read; append and rename fail before any disk access.
    """
    if operation not in {"append", "rotation"}:
        raise AssertionError("unknown fixture log operation")
    path = Path(__file__).resolve().parents[1] / "services" / "log_service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = [
        node for node in tree.body
        if (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        or (isinstance(node, ast.ClassDef) and node.name == "LogService")
    ]
    append = Mock(side_effect=OSError("synthetic-log-secret"))
    replace = Mock(side_effect=OSError("synthetic-log-secret"))
    namespace = {
        "__name__": "isolated_log_failure_fixture",
        "__builtins__": fixture.namespace["__builtins__"],
        "WeakValueDictionary": WeakValueDictionary,
        "Lock": fixture.namespace["Lock"],
        "datetime": fixture.namespace["datetime"],
        "json": fixture.namespace["json"],
        "uuid4": lambda: SimpleNamespace(hex="fixture-log-id"),
        "os": SimpleNamespace(SEEK_END=2, replace=replace),
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    cls = namespace["LogService"]
    logger = cls.__new__(cls)
    logger._lock = fixture.namespace["Lock"]()
    logger.max_bytes = 1
    logger.backup_count = 1

    class FailingAppend(BytesIO):
        def write(self, data):
            return append(data) if data else 0

    def open_log(mode):
        if mode == "rb":
            return BytesIO(b"{}\n" if operation == "rotation" else b"")
        if mode == "ab":
            return FailingAppend()
        raise AssertionError("unexpected fixture log access")

    backup = SimpleNamespace(unlink=Mock())
    logger.path = SimpleNamespace(
        name="fixture-log.jsonl", open=Mock(side_effect=open_log),
        with_name=Mock(return_value=backup),
    )
    logger.add = Mock(wraps=logger.add)
    fixture.namespace["log_service"] = logger
    return SimpleNamespace(logger=logger, append=append, replace=replace)


class NoPoolTraversal(dict):
    def __iter__(self):
        raise AssertionError("hot write traversed the pool")

    def keys(self):
        raise AssertionError("hot write copied the pool")

    def values(self):
        raise AssertionError("hot write materialized the pool")

    def items(self):
        raise AssertionError("hot write traversed the pool")


class IncrementalAccountTests(unittest.TestCase):
    def setUp(self):
        self.storage = MemoryIncrementalStorage([
            {"access_token": "fixture-a", "quota": 4, "refresh_token": "fixture-refresh-a"},
            {"access_token": "fixture-b", "quota": 3},
        ])
        self.fixture = load_isolated_service(self.storage)
        self.service = self.fixture.service

        def check_account_lock():
            acquired = self.service._lock.acquire(blocking=False)
            if acquired:
                self.service._lock.release()
            self.assertFalse(acquired, "persistence must remain under the account lock")

        self.storage.lock_probe = check_account_lock

    def assert_last_delta(self, upsert_tokens, deleted_tokens=()):
        upserts, deleted, snapshot = self.storage.change_calls[-1]
        self.assertEqual({item["access_token"] for item in upserts}, set(upsert_tokens))
        self.assertEqual(set(deleted), set(deleted_tokens))
        self.assertIsNone(snapshot)
        self.assertEqual(self.storage.save_calls, [])

    def test_hot_writes_only_persist_the_explicit_dirty_account(self):
        self.service._accounts = NoPoolTraversal(self.service._accounts)
        self.service.update_account("fixture-a", {"quota": 6}, quiet=True)
        self.assert_last_delta(["fixture-a"])
        self.service.mark_text_used("fixture-a")
        self.assert_last_delta(["fixture-a"])
        self.assertIsNotNone(self.storage.rows["fixture-a"]["last_used_at"])
        self.service._record_token_refresh_error("fixture-a", "fixture", "fixture-failure")
        self.assert_last_delta(["fixture-a"])
        self.service._record_invalid_token_seen("fixture-a", "fixture", "fixture-invalid")
        self.assert_last_delta(["fixture-a"])
        self.service._record_refresh_success("fixture-a")
        self.assert_last_delta(["fixture-a"])
        self.assertEqual(self.storage.rows["fixture-a"]["invalid_count"], 0)
        self.assertEqual(len(self.storage.change_calls), 5)

    def test_image_result_updates_quota_and_releases_slot_even_on_failure(self):
        token = self.service._acquire_next_candidate_token()
        result = self.service.mark_image_result(token, success=True, cooldown_secs=10)
        self.assertEqual(result["quota"], 3)
        self.assertEqual(result["success"], 1)
        self.assertNotIn(token, self.service._image_inflight)
        self.assertEqual(self.service._image_cooldown_until[token], 10)
        self.assert_last_delta([token])
        self.service.mark_image_result(token, success=False)
        self.assertEqual(self.service.get_account(token)["fail"], 1)
        before = self.service.get_account(token)
        self.service._image_inflight[token] = 1
        self.storage.fail = True
        with self.assertRaises(self.fixture.persistence_error):
            self.service.mark_image_result(token, success=True)
        self.assertEqual(self.service.get_account(token), before)
        self.assertNotIn(token, self.service._image_inflight)

    def test_batch_add_merge_and_delete_use_one_delta_each(self):
        result = self.service.add_account_items([
            {"access_token": "fixture-a", "quota": 9},
            {"access_token": "fixture-c", "quota": 2},
            {"access_token": "fixture-c", "type": "Plus"},
        ])
        self.assertEqual((result["added"], result["skipped"]), (1, 1))
        self.assert_last_delta(["fixture-a", "fixture-c"])
        self.assertEqual(self.storage.rows["fixture-c"]["quota"], 2)
        self.assertEqual(self.service._cumulative_total, 3)
        self.service.add_accounts(["fixture-d", "fixture-d", "fixture-e"])
        self.assert_last_delta(["fixture-d", "fixture-e"])
        self.assertEqual(len(self.storage.change_calls), 2)
        self.service._image_inflight["fixture-a"] = 2
        self.service._image_cooldown_until["fixture-a"] = 50
        self.service._index = 17
        result = self.service.delete_accounts(["fixture-a", "fixture-c", "fixture-c", "missing"])
        self.assertEqual(result["removed"], 2)
        self.assert_last_delta([], ["fixture-a", "fixture-c"])
        self.assertNotIn("fixture-a", self.service._image_inflight)
        self.assertNotIn("fixture-a", self.service._image_cooldown_until)
        self.assertLess(self.service._index, len(self.service._accounts))
        self.service.delete_accounts(["missing"])
        self.assertEqual(len(self.storage.change_calls), 3)

    def test_invalid_and_rate_limited_removal_only_delete_the_target(self):
        self.fixture.config.auto_remove_invalid_accounts = True
        self.assertTrue(self.service.remove_invalid_token("fixture-a", "fixture"))
        self.assert_last_delta([], ["fixture-a"])
        self.fixture.config.auto_remove_rate_limited_accounts = True
        self.service._image_inflight["fixture-b"] = 1
        self.service._image_cooldown_until["fixture-b"] = 30
        self.assertIsNone(self.service.update_account("fixture-b", {"status": "限流"}))
        self.assert_last_delta([], ["fixture-b"])
        self.assertEqual(self.service._index, 0)
        self.assertEqual(self.service._image_inflight, {})
        self.assertEqual(self.service._image_cooldown_until, {})

    def test_quota_exhaustion_auto_removes_and_cleans_alias_chain(self):
        self.fixture.config.auto_remove_rate_limited_accounts = True
        self.service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-a2"}, "fixture")
        self.service._apply_refreshed_tokens("fixture-a2", {"access_token": "fixture-a3"}, "fixture")
        self.service.update_account("fixture-a3", {"quota": 1})
        self.service._image_inflight["fixture-a3"] = 1
        self.assertIsNone(self.service.mark_image_result("fixture-a", success=True, cooldown_secs=20))
        self.assert_last_delta([], ["fixture-a3"])
        self.assertEqual(self.service._token_aliases, {})
        self.assertEqual(self.service._image_cooldown_until, {})
        self.assertEqual(self.service._image_inflight, {})

    def test_rotation_is_a_single_delete_upsert_and_migrates_slots(self):
        self.service._image_inflight["fixture-a"] = 2
        self.service._image_cooldown_until["fixture-a"] = 50
        new = self.service._apply_refreshed_tokens("fixture-a", {
            "access_token": "fixture-new", "refresh_token": "fixture-refresh-new",
        }, "fixture")
        self.assertEqual(new, "fixture-new")
        self.assert_last_delta(["fixture-new"], ["fixture-a"])
        self.assertEqual(self.service.resolve_access_token("fixture-a"), new)
        self.assertNotIn("fixture-a", self.storage.rows)
        self.assertEqual(self.service._image_inflight, {new: 2})
        self.assertEqual(self.service._image_cooldown_until, {new: 50})
        self.service.release_image_slot("fixture-a")
        self.assertEqual(self.service._image_inflight[new], 1)
        self.service.mark_text_used("fixture-a")
        self.assert_last_delta([new])
        self.service._apply_refreshed_tokens(new, {"access_token": new}, "fixture")
        self.assert_last_delta([new])

    def test_log_append_and_rotation_failures_do_not_undo_token_commit(self):
        for operation in ("append", "rotation"):
            with self.subTest(operation=operation):
                storage = MemoryIncrementalStorage([{"access_token": "fixture-old", "quota": 4}])
                fixture = load_isolated_service(storage)
                service = fixture.service
                failure = install_log_io_failure(fixture, operation)
                service._accounts = NoPoolTraversal(service._accounts)
                service._image_inflight["fixture-old"] = 2
                service._image_cooldown_until["fixture-old"] = 50

                def before_commit():
                    self.assertIn("fixture-old", service._accounts)
                    self.assertNotIn("fixture-new", service._accounts)
                    self.assertEqual(service._token_aliases, {})
                    self.assertEqual(service._image_inflight, {"fixture-old": 2})

                storage.lock_probe = before_commit
                new = service._apply_refreshed_tokens("fixture-old", {
                    "access_token": "fixture-new", "refresh_token": "fixture-new-refresh",
                }, "fixture")
                self.assertEqual(new, "fixture-new")
                self.assertEqual(set(storage.rows), {new})
                self.assertEqual(service.get_account(new), storage.rows[new])
                self.assertEqual(service.resolve_access_token("fixture-old"), new)
                self.assertEqual(service._image_inflight, {new: 2})
                self.assertEqual(service._image_cooldown_until, {new: 50})
                self.assertEqual(len(storage.change_calls), 1)
                self.assertEqual(storage.change_calls[0][1:], (["fixture-old"], None))
                self.assertEqual(storage.save_calls, [])
                failure.logger.add.assert_called_once()
                getattr(failure, "append" if operation == "append" else "replace").assert_called_once()
                getattr(failure, "replace" if operation == "append" else "append").assert_not_called()
                fixture.namespace["sys"].stderr.write.assert_called_once_with(
                    "AccountService: account log write failed\n",
                )

    def test_other_mutation_logs_are_best_effort_without_masking_storage_failure(self):
        for operation in ("append", "rotation"):
            with self.subTest(operation=operation):
                storage = MemoryIncrementalStorage()
                fixture = load_isolated_service(storage)
                service = fixture.service
                install_log_io_failure(fixture, operation)
                # Even the credential-free fallback diagnostic can fail.
                fixture.namespace["sys"].stderr.write.side_effect = OSError("synthetic-stderr-secret")
                service.add_accounts(["fixture-a", "fixture-b"])
                service.update_account("fixture-a", {"quota": 4})
                service._record_token_refresh_error("fixture-a", "fixture", "fixture-failure")
                service._record_invalid_token_seen("fixture-a", "fixture", "fixture-invalid")
                self.assertEqual(service.get_account("fixture-a"), storage.rows["fixture-a"])
                fixture.config.auto_remove_invalid_accounts = True
                self.assertTrue(service.remove_invalid_token("fixture-b", "fixture"))
                fixture.config.auto_remove_rate_limited_accounts = True
                self.assertIsNone(service.update_account("fixture-a", {"status": "限流"}))
                service.add_account_items([{"access_token": "fixture-c", "quota": 1}])
                self.assertIsNone(service.mark_image_result("fixture-c", success=True))
                self.assertEqual(storage.rows, {})
                self.assertEqual(service._accounts, {})
                self.assertEqual(storage.save_calls, [])
                self.assertTrue(all(call[2] is None for call in storage.change_calls))
                storage.fail = True
                with self.assertRaisesRegex(fixture.persistence_error, "^account persistence failed$"):
                    service.add_accounts(["fixture-not-committed"])
                self.assertEqual(storage.rows, {})
                self.assertEqual(service._accounts, {})

    def test_log_fallback_never_includes_payload_or_exception_details(self):
        self.fixture.namespace["log_service"].add.side_effect = OSError("synthetic-log-secret")
        self.service._log_account_event("synthetic-summary-secret", {
            "password": "synthetic-password", "access_token": "synthetic-access-token",
        })
        self.fixture.namespace["log_service"].add.assert_called_once()
        self.fixture.namespace["sys"].stderr.write.assert_called_once_with(
            "AccountService: account log write failed\n",
        )

    def test_rotation_into_existing_token_merges_inflight_and_cooldown(self):
        self.service._image_inflight.update({"fixture-a": 2, "fixture-b": 1})
        self.service._image_cooldown_until.update({"fixture-a": 50, "fixture-b": 70})
        self.service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-b"}, "fixture")
        self.assert_last_delta(["fixture-b"], ["fixture-a"])
        self.assertEqual(self.service._image_inflight, {"fixture-b": 3})
        self.assertEqual(self.service._image_cooldown_until, {"fixture-b": 70})
        self.storage.fail = True
        with self.assertRaises(self.fixture.persistence_error):
            self.service.delete_accounts(["fixture-a"])
        self.assertEqual(self.service.resolve_access_token("fixture-a"), "fixture-b")
        self.assertEqual(self.service._image_inflight, {"fixture-b": 3})
        self.storage.fail = False
        self.service.delete_accounts(["fixture-a"])
        self.assert_last_delta([], ["fixture-b"])
        self.assertEqual(self.service._token_aliases, {})
        self.assertEqual(self.service._image_inflight, {})
        self.assertEqual(self.service._image_cooldown_until, {})
        self.service.mark_image_result("fixture-b", success=False, cooldown_secs=30)
        self.assertEqual(self.service._image_cooldown_until, {})

    def test_failed_update_rotation_add_and_delete_leave_memory_uncommitted(self):
        self.service._image_inflight["fixture-a"] = 2
        self.service._image_cooldown_until["fixture-a"] = 50
        before = deepcopy(self.service._accounts)
        self.storage.fail = True
        operations = [
            lambda: self.service.update_account("fixture-a", {"quota": 99}),
            lambda: self.service.mark_text_used("fixture-a"),
            lambda: self.service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-new"}, "fixture"),
            lambda: self.service.delete_accounts(["fixture-a"]),
            lambda: self.service.add_accounts(["fixture-new"]),
        ]
        for operation in operations:
            with self.subTest(operation=operations.index(operation)):
                with self.assertRaisesRegex(self.fixture.persistence_error, "^account persistence failed$") as caught:
                    operation()
                self.assertNotIn("synthetic-storage-secret", str(caught.exception))
                self.assertEqual(self.service._accounts, before)
                self.assertEqual(self.service._image_inflight, {"fixture-a": 2})
                self.assertEqual(self.service._image_cooldown_until, {"fixture-a": 50})
                self.assertEqual(self.service._token_aliases, {})
                self.assertEqual(self.service._cumulative_total, 2)
        self.assertEqual(self.storage.save_calls, [])
        self.storage.fail = False
        self.service.update_account("fixture-b", {"quota": 7})
        self.assert_last_delta(["fixture-b"])
        self.assertEqual(self.storage.rows["fixture-a"]["quota"], 4)

    def test_implemented_method_failure_never_falls_back_even_if_not_implemented(self):
        self.storage.apply_account_changes = Mock(side_effect=NotImplementedError("fixture"))
        with self.assertRaises(self.fixture.persistence_error):
            self.service.mark_text_used("fixture-a")
        self.assertEqual(self.storage.save_calls, [])

    def test_legacy_and_inherited_snapshot_fallback_include_post_change_pool(self):
        for cls in (LegacyMemoryStorage, SnapshotMemoryStorage):
            with self.subTest(storage=cls.__name__):
                storage = cls([{"access_token": "fixture-a"}, {"access_token": "fixture-b"}])
                fixture = load_isolated_service(storage)
                service = fixture.service
                service.update_account("fixture-a", {"quota": 5})
                self.assertEqual(set(storage.rows), {"fixture-a", "fixture-b"})
                service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-new"}, "fixture")
                self.assertEqual(set(storage.rows), {"fixture-new", "fixture-b"})
                self.assertEqual(storage.rows["fixture-new"]["quota"], 5)
                service.delete_accounts(["fixture-b"])
                self.assertEqual(set(storage.rows), {"fixture-new"})
                before = service.get_account("fixture-new")
                storage.fail = True
                with self.assertRaises(fixture.persistence_error):
                    service.mark_text_used("fixture-new")
                self.assertEqual(service.get_account("fixture-new"), before)

    def test_fetch_remote_fixture_combines_success_and_quota_in_one_write(self):
        class FixtureInvalidToken(Exception):
            pass

        backend = SimpleNamespace(get_user_info=lambda: {"quota": 8, "status": "正常"}, close=Mock())
        self.fixture.dependencies["services.openai_backend_api"] = SimpleNamespace(
            InvalidAccessTokenError=FixtureInvalidToken, OpenAIBackendAPI=lambda token: backend,
        )
        self.service.fetch_remote_info("fixture-a")
        self.assertEqual(len(self.storage.change_calls), 1)
        self.assert_last_delta(["fixture-a"])
        self.assertEqual(self.storage.rows["fixture-a"]["quota"], 8)
        self.assertEqual(self.storage.rows["fixture-a"]["invalid_count"], 0)
        backend.close.assert_called_once()

    def test_persistence_failure_not_swallowed_by_candidate_selection(self):
        self.service.fetch_remote_info = Mock(side_effect=self.fixture.persistence_error("account persistence failed"))
        with self.assertRaises(self.fixture.persistence_error):
            self.service.get_available_access_token()
        self.assertEqual(self.service._image_inflight, {})
        self.service.fetch_remote_info.assert_called_once()

    def test_refresh_batch_reports_persistence_failure_and_finishes(self):
        self.storage.fail = True
        self.service.fetch_remote_info = lambda token, *args: self.service.update_account(token, {"quota": 8})
        result = self.service.refresh_accounts(["fixture-a"], "fixture-progress")
        self.assertEqual(result["refreshed"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["error"], "account persistence failed")
        progress = self.service.get_refresh_progress("fixture-progress")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(len(progress["result"]["errors"]), 1)
        self.assertEqual(self.service.get_account("fixture-a")["quota"], 4)

    def test_concurrent_rotation_and_slot_release_keep_runtime_keys_consistent(self):
        self.service._image_inflight["fixture-a"] = 40
        self.service._image_cooldown_until["fixture-a"] = 50

        def operation(index):
            if index % 2:
                self.service.release_image_slot("fixture-a")
            else:
                self.service._apply_refreshed_tokens(
                    "fixture-a", {"access_token": f"fixture-rotation-{index}"}, "fixture",
                )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(operation, range(80)))
        final = self.service.resolve_access_token("fixture-a")
        self.assertNotEqual(final, "fixture-a")
        self.assertEqual(self.service._image_inflight, {})
        self.assertEqual(self.service._image_cooldown_until, {final: 50})
        self.assertEqual(set(self.storage.rows), {final, "fixture-b"})
        self.assertEqual(len(self.storage.change_calls), 40)
        self.assertTrue(all(len(call[0]) == len(call[1]) == 1 for call in self.storage.change_calls))

    def test_concurrent_results_do_not_lose_counters_or_quota(self):
        self.service.update_account("fixture-a", {"quota": 100})
        self.storage.change_calls.clear()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: self.service.mark_image_result("fixture-a", success=True), range(40)))
        account = self.service.get_account("fixture-a")
        self.assertEqual((account["quota"], account["success"]), (60, 40))
        self.assertEqual(len(self.storage.change_calls), 40)
        self.assertEqual(self.storage.rows["fixture-a"]["quota"], 60)
        self.assertTrue(all(len(call[0]) == 1 and not call[1] and call[2] is None for call in self.storage.change_calls))


if __name__ == "__main__":
    unittest.main()
