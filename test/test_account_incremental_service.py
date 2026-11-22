"""Synthetic fixtures for the coordinator's isolated source-copy test runner.

These import the real modules and must not run in the live working directory.
Fixture results do not establish real authentication or purchase behavior.
"""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import services.account_service as account_service_module
from services.account_service import AccountPersistenceError, AccountService, ProgressCapacityError
from services.storage.base import StorageBackend


class SnapshotMemoryStorage(StorageBackend):
    """In-memory persistence using the real base snapshot implementation."""

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

    def load_auth_keys(self):
        return []

    def save_auth_keys(self, auth_keys):
        pass

    def health_check(self):
        return {"ok": True}

    def get_backend_info(self):
        return {"type": "memory"}


class MemoryIncrementalStorage(SnapshotMemoryStorage):
    def __init__(self, accounts=()):
        super().__init__(accounts)
        self.change_calls = []
        self.lock_probe = lambda: None

    def apply_account_changes(
        self, upserts: list[dict], deleted_tokens: list[str], *, snapshot: list[dict] | None = None,
    ) -> None:
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


@contextmanager
def account_service_fixture(storage=None):
    config = SimpleNamespace(
        image_account_concurrency=4,
        auto_remove_invalid_accounts=False,
        auto_remove_rate_limited_accounts=False,
        auto_relogin_after_refresh=False,
    )
    clock = Clock()
    log_add = Mock()
    stderr = Mock()
    with ExitStack() as patches:
        patches.enter_context(patch.object(account_service_module, "config", config))
        # Replace only this module's binding, never the standard-library time module.
        patches.enter_context(patch.object(account_service_module, "time", clock))
        patches.enter_context(patch.object(account_service_module, "log_service", SimpleNamespace(add=log_add)))
        patches.enter_context(patch.object(
            account_service_module, "sys", SimpleNamespace(stderr=SimpleNamespace(write=stderr)),
        ))
        patches.enter_context(patch.object(AccountService, "_load_cumulative_total", lambda self: len(self._accounts)))
        patches.enter_context(patch.object(AccountService, "_save_cumulative_total"))
        patches.enter_context(patch.object(
            AccountService, "_login_with_password",
            side_effect=AssertionError("real authentication forbidden in fixture"),
        ))
        patches.enter_context(patch.object(
            AccountService, "_request_access_token_refresh",
            side_effect=AssertionError("real authentication forbidden in fixture"),
        ))
        service = AccountService(storage if storage is not None else MemoryIncrementalStorage())
        yield SimpleNamespace(
            service=service, config=config, clock=clock, log_add=log_add, stderr=stderr,
            persistence_error=AccountPersistenceError, capacity_error=ProgressCapacityError,
        )


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
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.fixture = self.patches.enter_context(account_service_fixture(self.storage))
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
        self.assertEqual(self.storage.rows["fixture-a"]["invalid_count"], 1)
        self.assertEqual(len(self.storage.change_calls), 4)

    def test_import_merges_records_of_the_same_upstream_account_and_plan(self):
        def jwt(exp: int, name: str) -> str:
            import base64, json
            body = base64.urlsafe_b64encode(json.dumps({"exp": exp, "n": name}).encode()).decode().rstrip("=")
            return f"h.{body}.s"

        old, new, team = jwt(100, "old"), jwt(200, "new"), jwt(50, "team")
        self.service._add_account_payloads([
            {"access_token": old, "user_id": "user-1", "type": "free", "created_at": "2026-01-01 00:00:00",
             "success": 5, "fail": 2, "upload_limited_until": "2099-01-01T00:00:00+00:00"},
            {"access_token": team, "user_id": "user-1", "type": "team"},
            {"access_token": new, "user_id": "user-1", "type": "free", "created_at": "2026-02-01 00:00:00",
             "success": 1, "fail": 0},
        ])

        self.assertEqual(self.service.merge_duplicate_accounts([new]), 1)
        merged = self.service.get_account(new)
        self.assertEqual(
            (merged["success"], merged["fail"], merged["created_at"], merged["upload_limited_until"]),
            (6, 2, "2026-01-01 00:00:00", "2099-01-01T00:00:00+00:00"),
        )
        self.assertIsNone(self.service.get_account(old))
        self.assertNotIn(old, self.storage.rows)
        # 套餐不同的记录保留。
        self.assertIsNotNone(self.service.get_account(team))

        # 导入的 token 比已有的旧：保留已有的那条，丢掉导入的。
        older = jwt(150, "older")
        self.service._add_account_payloads([{"access_token": older, "user_id": "user-1", "type": "free"}])
        self.assertEqual(self.service.merge_duplicate_accounts([older]), 1)
        self.assertIsNone(self.service.get_account(older))
        self.assertIsNotNone(self.service.get_account(new))

    def test_upload_limited_account_is_skipped_only_for_requests_with_reference_images(self):
        self.service._accounts["fixture-a"]["quota"] = 4
        self.service._accounts["fixture-b"]["quota"] = 4
        self.service.mark_upload_limited("fixture-a", 3600)

        self.assertEqual(self.service._list_ready_candidate_tokens(needs_upload=True), ["fixture-b"])
        self.assertEqual(self.service._list_ready_candidate_tokens(), ["fixture-a", "fixture-b"])
        # 恢复时间随账号数据落库，重启后仍然生效。
        self.assertEqual(self.storage.rows["fixture-a"]["upload_limited_until"], "2023-11-14T23:13:20+00:00")
        stats = self.service.get_stats()
        self.assertEqual((stats["upload_limited"], stats["total_quota"], stats["upload_quota"]), (1, 8, 4))

        self.service.mark_upload_limited("fixture-b", 7200)
        with self.assertRaisesRegex(RuntimeError, "reference images.*earliest recovery at 2023-11-14T23:13:20"):
            self.service._acquire_next_candidate_token(needs_upload=True)

        self.fixture.clock.now = 3600
        self.assertEqual(self.service._list_ready_candidate_tokens(needs_upload=True), ["fixture-a"])
        self.assertEqual(self.service.get_stats()["upload_limited"], 1)

    def test_candidate_selection_scans_ready_once_and_preserves_round_robin(self):
        self.service._index = 1
        with patch.object(
            self.service, "_list_ready_candidate_tokens", wraps=self.service._list_ready_candidate_tokens,
        ) as ready:
            token = self.service._acquire_next_candidate_token()
        ready.assert_called_once_with(None, None, None, None, False)
        self.assertEqual(token, "fixture-b")
        self.assertEqual(self.service._index, 2)
        self.assertEqual(self.service._image_inflight, {"fixture-b": 1})

    def test_candidate_selection_recomputes_ready_after_busy_wait(self):
        for blocker in ("concurrency", "cooldown"):
            with self.subTest(blocker=blocker):
                self.service._accounts["fixture-a"]["quota"] = 4
                self.service._accounts["fixture-b"]["quota"] = 0
                self.service._image_inflight = (
                    {"fixture-a": self.fixture.config.image_account_concurrency} if blocker == "concurrency" else {}
                )
                self.service._image_cooldown_until = {"fixture-a": 10} if blocker == "cooldown" else {}

                def change_ready_after_wait(*, timeout):
                    self.assertEqual(wait.call_count, 1, "candidate selection must use the updated ready list")
                    self.assertTrue(self.service._lock.locked())
                    self.service._accounts["fixture-a"]["quota"] = 0
                    self.service._accounts["fixture-b"]["quota"] = 3

                with patch.object(
                    self.service, "_list_ready_candidate_tokens", wraps=self.service._list_ready_candidate_tokens,
                ) as ready, patch.object(
                    self.service._image_slot_condition, "wait", side_effect=change_ready_after_wait,
                ) as wait:
                    self.assertEqual(self.service._acquire_next_candidate_token(), "fixture-b")
                wait.assert_called_once_with(timeout=1.0)
                self.assertEqual(ready.call_count, 2)

    def test_candidate_selection_without_ready_accounts_does_not_wait(self):
        for account in self.service._accounts.values():
            account["quota"] = 0
        with patch.object(
            self.service, "_list_ready_candidate_tokens", wraps=self.service._list_ready_candidate_tokens,
        ) as ready, patch.object(
            self.service._image_slot_condition, "wait", side_effect=AssertionError("unexpected wait"),
        ) as wait:
            with self.assertRaisesRegex(RuntimeError, "^no available image quota$"):
                self.service._acquire_next_candidate_token()
        ready.assert_called_once_with(None, None, None, None, False)
        wait.assert_not_called()

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

    def test_log_failure_does_not_undo_token_commit(self):
        self.fixture.log_add.side_effect = OSError("synthetic-log-secret")
        self.service._accounts = NoPoolTraversal(self.service._accounts)
        self.service._image_inflight["fixture-a"] = 2
        self.service._image_cooldown_until["fixture-a"] = 50
        check_account_lock = self.storage.lock_probe

        def before_commit():
            check_account_lock()
            self.assertIn("fixture-a", self.service._accounts)
            self.assertNotIn("fixture-new", self.service._accounts)
            self.assertEqual(self.service._token_aliases, {})
            self.assertEqual(self.service._image_inflight, {"fixture-a": 2})

        self.storage.lock_probe = before_commit
        new = self.service._apply_refreshed_tokens("fixture-a", {
            "access_token": "fixture-new", "refresh_token": "fixture-new-refresh",
        }, "fixture")
        self.assertEqual(new, "fixture-new")
        self.assertEqual(set(self.storage.rows), {new, "fixture-b"})
        self.assertEqual(self.service.get_account(new), self.storage.rows[new])
        self.assertEqual(self.service.resolve_access_token("fixture-a"), new)
        self.assertEqual(self.service._image_inflight, {new: 2})
        self.assertEqual(self.service._image_cooldown_until, {new: 50})
        self.assertEqual(len(self.storage.change_calls), 1)
        self.assert_last_delta([new], ["fixture-a"])
        self.fixture.log_add.assert_called_once()
        self.fixture.stderr.assert_called_once_with("AccountService: account log write failed\n")

    def test_other_mutation_logs_are_best_effort_without_masking_storage_failure(self):
        with account_service_fixture() as fixture:
            service = fixture.service
            storage = service.storage
            fixture.log_add.side_effect = OSError("synthetic-log-secret")
            # Even the credential-free fallback diagnostic can fail.
            fixture.stderr.side_effect = OSError("synthetic-stderr-secret")
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
        self.fixture.log_add.side_effect = OSError("synthetic-log-secret")
        self.service._log_account_event("synthetic-summary-secret", {
            "password": "synthetic-password", "access_token": "synthetic-access-token",
        })
        self.fixture.log_add.assert_called_once()
        self.fixture.stderr.assert_called_once_with("AccountService: account log write failed\n")

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
        with patch.object(self.storage, "apply_account_changes", side_effect=NotImplementedError("fixture")):
            with self.assertRaises(self.fixture.persistence_error):
                self.service.mark_text_used("fixture-a")
        self.assertEqual(self.storage.save_calls, [])

    def test_inherited_base_snapshot_includes_post_change_pool(self):
        storage = SnapshotMemoryStorage([{"access_token": "fixture-a"}, {"access_token": "fixture-b"}])
        with account_service_fixture(storage) as fixture:
            service = fixture.service
            # The supplied snapshot must avoid reloading even for a full-snapshot backend.
            with patch.object(storage, "load_accounts", side_effect=AssertionError("unexpected reload")):
                service.update_account("fixture-a", {"quota": 5})
                self.assertEqual(set(storage.rows), {"fixture-a", "fixture-b"})
                service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-new"}, "fixture")
                self.assertEqual(set(storage.rows), {"fixture-new", "fixture-b"})
                self.assertEqual(storage.rows["fixture-new"]["quota"], 5)
                service.delete_accounts(["fixture-b"])
                self.assertEqual(set(storage.rows), {"fixture-new"})
                self.assertEqual(len(storage.save_calls), 3)
                before = service.get_account("fixture-new")
                storage.fail = True
                with self.assertRaises(fixture.persistence_error):
                    service.mark_text_used("fixture-new")
                self.assertEqual(service.get_account("fixture-new"), before)
                self.assertEqual(storage.rows["fixture-new"], before)

    def test_fetch_remote_fixture_combines_success_and_quota_in_one_write(self):
        self.service._accounts["fixture-a"].update({
            "invalid_count": 2,
            "last_invalid_at": "2024-01-01T00:00:00+00:00",
            "last_refresh_error": "fixture-invalid",
            "last_refresh_error_at": "2024-01-01T00:00:00+00:00",
        })
        self.service._accounts = NoPoolTraversal(self.service._accounts)
        backend = Mock()
        backend.get_user_info.return_value = {"quota": 8, "status": "正常"}
        with patch("services.openai_backend_api.OpenAIBackendAPI", return_value=backend) as backend_class:
            self.service.fetch_remote_info("fixture-a")
        backend_class.assert_called_once_with("fixture-a")
        self.assertEqual(len(self.storage.change_calls), 1)
        self.assert_last_delta(["fixture-a"])
        self.assertEqual(self.storage.rows["fixture-a"]["quota"], 8)
        self.assertEqual(self.storage.rows["fixture-a"]["invalid_count"], 0)
        for field in ("last_invalid_at", "last_refresh_error", "last_refresh_error_at"):
            self.assertIsNone(self.storage.rows["fixture-a"][field])
        backend.close.assert_called_once()

    def test_persistence_failure_not_swallowed_by_candidate_selection(self):
        with patch.object(
            self.service, "fetch_remote_info",
            side_effect=self.fixture.persistence_error("account persistence failed"),
        ) as fetch:
            with self.assertRaises(self.fixture.persistence_error):
                self.service.get_available_access_token()
        self.assertEqual(self.service._image_inflight, {})
        fetch.assert_called_once()

    def test_refresh_batch_reports_persistence_failure_and_finishes(self):
        self.storage.fail = True
        with patch.object(
            self.service, "fetch_remote_info",
            side_effect=lambda token, *args: self.service.update_account(token, {"quota": 8}),
        ):
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
