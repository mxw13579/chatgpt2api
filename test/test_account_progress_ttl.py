"""Isolated progress/API fixtures: no app import, real credentials, HTTP or login.

Execution is intentionally left to the coordinator's approved isolated test run.
"""
from __future__ import annotations

import ast
import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from test.test_account_incremental_service import (
    MemoryIncrementalStorage,
    install_log_io_failure,
    load_isolated_service,
)


class ProgressTTLTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_isolated_service(MemoryIncrementalStorage([
            {"access_token": "fixture-a", "quota": 3, "password": "fixture-old-password"},
        ]))
        self.service = self.fixture.service
        self.clock = self.fixture.clock

    def methods(self, kind):
        return (
            getattr(self.service, f"init_{kind}_progress"),
            getattr(self.service, f"finish_{kind}_progress"),
            getattr(self.service, f"get_{kind}_progress"),
        )

    def test_defaults_and_instance_state_are_isolated(self):
        self.assertEqual(self.service._PROGRESS_TTL_SECONDS, 1800)
        self.assertEqual(self.service._PROGRESS_MAX_ENTRIES, 128)
        # 两个实例必须使用同一个类，才能捕获类级字典/锁共享回归。
        other = type(self.service)(MemoryIncrementalStorage())
        self.service.init_refresh_progress("same-id", 1)
        self.service.init_relogin_progress("same-id", 2)
        self.assertIsNone(other.get_refresh_progress("same-id"))
        self.assertIsNone(other.get_relogin_progress("same-id"))
        self.assertIsNot(other._progress_lock, self.service._progress_lock)
        other.init_refresh_progress("same-id", 7)
        self.assertEqual(self.service.get_refresh_progress("same-id")["total"], 1)
        self.assertEqual(other.get_refresh_progress("same-id")["total"], 7)

    def test_ttl_boundary_is_measured_from_completion_not_creation_or_polling(self):
        for kind in ("refresh", "relogin"):
            with self.subTest(kind=kind):
                init, finish, get = self.methods(kind)
                self.clock.now = 0
                init(kind, 1)
                self.clock.now = 5000
                self.assertFalse(get(kind)["done"], "active tasks must not expire")
                finish(kind, {"refreshed": 1, "errors": []})
                self.clock.now = 6799.999
                self.assertTrue(get(kind)["done"])
                # Late duplicate callbacks must not extend retention.
                finish(kind, {"refreshed": 99, "errors": []})
                self.clock.now = 6800
                self.assertIsNone(get(kind))
                self.assertNotIn(kind, getattr(self.service, f"_{kind}_progress"))

    def test_relogin_automatic_completion_sets_ttl_and_ignores_late_updates(self):
        self.service.init_relogin_progress("relogin", 1)
        self.clock.now = 25
        self.service.update_relogin_progress("relogin", "fixture-a", "成功")
        progress = self.service.get_relogin_progress("relogin")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["result"]["relogined"], 1)
        self.clock.now = 1824.999
        self.service.update_relogin_progress("relogin", "fixture-a", "异常", "fixture-error")
        self.assertEqual(self.service.get_relogin_progress("relogin")["processed"], 1)
        self.clock.now = 1825
        self.assertIsNone(self.service.get_relogin_progress("relogin"))

    def test_get_init_update_and_finish_really_prune_both_caches(self):
        triggers = [
            lambda: self.service.get_refresh_progress("missing"),
            lambda: self.service.get_relogin_progress("missing"),
            lambda: self.service.init_refresh_progress("new-refresh", 1),
            lambda: self.service.init_relogin_progress("new-relogin", 1),
            lambda: self.service.update_refresh_progress("missing", "fixture-a"),
            lambda: self.service.update_relogin_progress("missing", "fixture-a", "成功"),
            lambda: self.service.finish_refresh_progress("missing"),
            lambda: self.service.finish_relogin_progress("missing"),
        ]
        for index, trigger in enumerate(triggers):
            with self.subTest(trigger=index):
                self.clock.now = index * 4000
                self.service.init_refresh_progress("expired-refresh", 0)
                self.service.finish_refresh_progress("expired-refresh")
                self.service.init_relogin_progress("expired-relogin", 0)
                self.service.finish_relogin_progress("expired-relogin")
                self.clock.now += 1800
                trigger()
                self.assertNotIn("expired-refresh", self.service._refresh_progress)
                self.assertNotIn("expired-relogin", self.service._relogin_progress)

    def test_shared_capacity_rejects_without_eviction_and_recovers_after_ttl(self):
        self.service._PROGRESS_MAX_ENTRIES = 2
        self.service.init_refresh_progress("active", 1)
        self.service.init_relogin_progress("completed", 0)
        self.service.finish_relogin_progress("completed")
        self.clock.now = 1799
        with self.assertRaises(self.fixture.capacity_error):
            self.service.init_refresh_progress("rejected", 1)
        self.assertIsNone(self.service.get_refresh_progress("rejected"))
        self.assertIsNotNone(self.service.get_refresh_progress("active"))
        self.assertIsNotNone(self.service.get_relogin_progress("completed"))
        self.clock.now = 1800
        self.service.init_refresh_progress("accepted", 1)
        self.assertNotIn("completed", self.service._relogin_progress)
        self.clock.now = 1_000_000
        with self.assertRaises(self.fixture.capacity_error):
            self.service.init_relogin_progress("still-full", 1)
        self.assertIsNotNone(self.service.get_refresh_progress("active"))
        self.assertIsNotNone(self.service.get_refresh_progress("accepted"))
        self.service.clean_refresh_progress("active")
        self.assertIsNotNone(self.service.get_refresh_progress("active"))
        self.service.finish_refresh_progress("accepted")
        self.service.clean_refresh_progress("accepted")
        self.service.init_relogin_progress("active-relogin", 1)
        self.service.clean_relogin_progress("active-relogin")
        self.assertIsNotNone(self.service.get_relogin_progress("active-relogin"))

    def test_duplicate_init_does_not_reset_active_or_completed_records(self):
        self.service._PROGRESS_MAX_ENTRIES = 2
        self.service.init_refresh_progress("refresh", 2)
        self.service.update_refresh_progress("refresh", "fixture-a")
        self.service.init_relogin_progress("relogin", 2)
        self.service.update_relogin_progress("relogin", "fixture-a", "成功")
        for kind in ("refresh", "relogin"):
            init, finish, get = self.methods(kind)
            before = get(kind)
            init(kind, 999)
            self.assertEqual(get(kind), before)
            finish(kind, {"refreshed": 1, "errors": []})
            init(kind, 999)
            self.assertTrue(get(kind)["done"])
            self.assertEqual(get(kind)["total"], 2)

    def test_only_summaries_are_cached_items_are_rebuilt_from_current_accounts(self):
        self.service.init_refresh_progress("refresh", 1)
        self.service.update_refresh_progress("refresh", "fixture-a")
        result = {
            "refreshed": 1, "relogined": 2, "skipped": 3,
            "items": self.service.list_accounts(),
            "password": "fixture-extra-secret",
            "errors": [{
                "access_token": "fixture-old-access", "password": "fixture-extra-secret",
                "error": "fixture-old-password fixture-old-refresh",
            }],
        }
        self.service.finish_refresh_progress("refresh", result)
        cached = self.service._refresh_progress["refresh"]
        self.assertNotIn("items", cached["result"])
        self.assertEqual(set(cached["result"]), {"refreshed", "relogined", "skipped", "errors"})
        for secret in ("fixture-old-access", "fixture-old-password", "fixture-old-refresh", "fixture-extra-secret"):
            self.assertNotIn(secret, repr(cached))
        result["errors"].clear()
        self.service._apply_refreshed_tokens("fixture-a", {"access_token": "fixture-current"}, "fixture")
        self.service.update_account("fixture-current", {"password": "fixture-new-password", "quota": 9})
        progress = self.service.get_refresh_progress("refresh")
        self.assertEqual(progress["status_counts"]["正常"], 1)
        self.assertEqual(progress["total_quota"], 3)
        self.assertEqual(progress["result"]["refreshed"], 1)
        self.assertEqual(progress["result"]["relogined"], 2)
        self.assertEqual(progress["result"]["skipped"], 3)
        self.assertEqual(len(progress["result"]["errors"]), 1)
        self.assertEqual(progress["result"]["items"][0]["access_token"], "fixture-current")
        self.assertEqual(progress["result"]["items"][0]["quota"], 9)
        self.assertNotIn("_finished_at", progress)
        progress["status_counts"]["正常"] = 999
        progress["result"]["errors"].clear()
        progress["result"]["items"].clear()
        again = self.service.get_refresh_progress("refresh")
        self.assertEqual(again["status_counts"]["正常"], 1)
        self.assertEqual(len(again["result"]["errors"]), 1)
        self.assertEqual(len(again["result"]["items"]), 1)
        self.assertNotIn("items", cached["result"])

    def test_relogin_results_and_fatal_errors_never_cache_credentials(self):
        self.service.init_relogin_progress("relogin", 2)
        self.service.update_relogin_progress("relogin", "fixture-old-access", "异常", "fixture-password")
        response = self.service.get_relogin_progress("relogin")
        response["results"][0]["status"] = "mutated"
        response["results"].append({})
        self.assertEqual(len(self.service.get_relogin_progress("relogin")["results"]), 1)
        self.service.update_relogin_progress("relogin", "fixture-a", "跳过", "fixture-refresh-secret")
        cache = self.service._relogin_progress["relogin"]
        self.assertTrue(cache["done"])
        self.assertNotIn("items", cache["result"])
        for secret in ("fixture-old-access", "fixture-password", "fixture-refresh-secret"):
            self.assertNotIn(secret, repr(cache))
        self.service.init_refresh_progress("fatal", 1)
        self.service.finish_refresh_progress("fatal", error="fixture-fatal-credential")
        self.assertEqual(self.service.get_refresh_progress("fatal")["error"], "account operation failed")
        self.assertNotIn("fixture-fatal-credential", repr(self.service._refresh_progress))

    def test_account_lock_is_never_acquired_while_holding_progress_lock(self):
        def assert_progress_lock_free():
            acquired = self.service._progress_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                self.service._progress_lock.release()

        get_account = self.service.get_account
        list_accounts = self.service.list_accounts

        def checked_get(token):
            assert_progress_lock_free()
            return get_account(token)

        def checked_list():
            assert_progress_lock_free()
            return list_accounts()

        self.service.get_account = checked_get
        self.service.list_accounts = checked_list
        self.service.init_refresh_progress("refresh", 1)
        self.service.update_refresh_progress("refresh", "fixture-a")
        self.service.finish_refresh_progress("refresh", {"refreshed": 1, "errors": []})
        self.service.get_refresh_progress("refresh")
        self.service.init_relogin_progress("relogin", 1)
        self.service.update_relogin_progress("relogin", "fixture-a", "成功")
        self.service.get_relogin_progress("relogin")

    def test_concurrent_updates_reads_and_admissions_are_bounded(self):
        self.service.init_refresh_progress("refresh", 100)
        self.service.init_relogin_progress("relogin", 100)

        def update(index):
            self.service.update_refresh_progress("refresh", "fixture-a")
            self.service.update_relogin_progress("relogin", f"fixture-{index}", "成功")
            self.service.get_refresh_progress("refresh")["status_counts"].clear()
            self.service.get_relogin_progress("relogin")["results"].clear()

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(update, range(100)))
        refresh = self.service.get_refresh_progress("refresh")
        relogin = self.service.get_relogin_progress("relogin")
        self.assertEqual(refresh["processed"], 100)
        self.assertEqual(refresh["status_counts"]["正常"], 100)
        self.assertEqual(refresh["total_quota"], 300)
        self.assertEqual(relogin["processed"], 100)
        self.assertEqual(len(relogin["results"]), 100)
        self.assertTrue(relogin["done"])
        self.service._PROGRESS_MAX_ENTRIES = 8

        def admit(index):
            try:
                self.service.init_refresh_progress(f"new-{index}", 1)
            except self.fixture.capacity_error:
                return False
            return True

        with ThreadPoolExecutor(max_workers=8) as executor:
            accepted = list(executor.map(admit, range(32)))
        self.assertEqual(sum(accepted), 6)
        self.assertEqual(len(self.service._refresh_progress) + len(self.service._relogin_progress), 8)
        self.assertIsNotNone(self.service.get_refresh_progress("refresh"))

    def test_empty_workers_still_create_completed_records(self):
        self.service.refresh_accounts([], "empty-refresh")
        self.service.re_login_accounts([], "empty-relogin")
        for kind in ("refresh", "relogin"):
            progress = getattr(self.service, f"get_{kind}_progress")(f"empty-{kind}")
            self.assertTrue(progress["done"])
            self.assertEqual(progress["total"], 0)
            self.assertIsNotNone(progress["result"])

    def test_executor_start_failure_finishes_before_propagating(self):
        self.fixture.namespace["ThreadPoolExecutor"] = Mock(side_effect=RuntimeError("fixture-start-failure"))
        with self.assertRaisesRegex(RuntimeError, "fixture-start-failure"):
            self.service.refresh_accounts(["fixture-a"], "start-failure")
        self.assertTrue(self.service.get_refresh_progress("start-failure")["done"])

    def test_relogin_workers_finish_once_and_expire_despite_log_io_failures(self):
        outcomes = [
            ("success", {"ok": True, "access_token": "fixture-new", "refresh_token": "fixture-new-refresh"}, "成功"),
            ("rejected", {"ok": False, "error": "invalid_password"}, "异常"),
            ("other-403", {"ok": False, "error": "password_verify_failed_403",
                           "detail": {"error": {"code": "invalid_credentials"}}}, "异常"),
            ("deactivated", {"ok": False, "error": "password_verify_failed_403",
                             "detail": {"error": {"code": "account_deactivated"},
                                        "access_token": "synthetic-detail-secret"}}, "禁用"),
            ("exception", RuntimeError("synthetic-auth-secret"), "异常"),
        ]
        for operation in ("append", "rotation"):
            for label, outcome, status in outcomes:
                for stderr_fails in (False, True):
                    with self.subTest(operation=operation, outcome=label, stderr_fails=stderr_fails):
                        fixture = load_isolated_service(MemoryIncrementalStorage([{
                            "access_token": "fixture-a", "quota": 3, "status": "异常",
                            "password": "fixture-password", "refresh_token": "fixture-refresh",
                        }]))
                        service = fixture.service
                        fixture.config.auto_remove_invalid_accounts = True
                        failure = install_log_io_failure(fixture, operation)
                        stderr = fixture.namespace["sys"].stderr.write
                        if stderr_fails:
                            stderr.side_effect = OSError("synthetic-stderr-secret")
                        service._login_with_password = (
                            Mock(side_effect=outcome) if isinstance(outcome, Exception)
                            else Mock(return_value=outcome)
                        )
                        service.remove_invalid_token = Mock(wraps=service.remove_invalid_token)
                        service.update_relogin_progress = Mock(wraps=service.update_relogin_progress)
                        service._PROGRESS_MAX_ENTRIES = 1
                        service.init_relogin_progress("worker", 1)
                        fixture.clock.now = 25
                        service._password_re_login_thread(
                            "fixture-a", "fixture@example.invalid", "fixture-password", "fixture", "worker",
                        )
                        error = "account operation failed" if status == "异常" else None
                        service.update_relogin_progress.assert_called_once_with("worker", "fixture-a", status, error)
                        progress = service.get_relogin_progress("worker")
                        self.assertTrue(progress["done"])
                        self.assertEqual(progress["processed"], 1)
                        self.assertEqual(len(progress["results"]), 1)
                        self.assertEqual(progress["results"][0]["status"], status)
                        self.assertEqual(progress["result"]["relogined"], int(status == "成功"))
                        self.assertEqual(len(progress["result"]["errors"]), int(status == "异常"))
                        if status == "异常":
                            service.remove_invalid_token.assert_called_once()
                            self.assertEqual(service.storage.rows, {})
                            self.assertEqual(service._accounts, {})
                        else:
                            service.remove_invalid_token.assert_not_called()
                            account = service.get_account("fixture-a")
                            self.assertEqual(account["status"], "正常" if status == "成功" else "禁用")
                            self.assertEqual(account["quota"], 3 if status == "成功" else 0)
                            self.assertEqual(account["password"], "fixture-password")
                            self.assertEqual(account, service.storage.rows[account["access_token"]])
                            if status == "成功":
                                self.assertEqual(account["access_token"], "fixture-new")
                                self.assertEqual(account["refresh_token"], "fixture-new-refresh")
                                self.assertEqual(account["source_type"], "password")
                                self.assertEqual(len(service.storage.change_calls), 2)
                        getattr(failure, "append" if operation == "append" else "replace").assert_called()
                        getattr(failure, "replace" if operation == "append" else "append").assert_not_called()
                        self.assertEqual(stderr.call_count, failure.logger.add.call_count)
                        for call in stderr.call_args_list:
                            self.assertEqual(call.args, ("AccountService: account log write failed\n",))
                            self.assertEqual(call.kwargs, {})
                        for secret in ("synthetic-auth-secret", "synthetic-detail-secret", "synthetic-log-secret"):
                            self.assertNotIn(secret, repr(failure.logger.add.call_args_list))
                            self.assertNotIn(secret, repr(service._relogin_progress))
                        fixture.clock.now = 1824.999
                        with self.assertRaises(fixture.capacity_error):
                            service.init_refresh_progress("too-early", 1)
                        fixture.clock.now = 1825
                        service.init_refresh_progress("capacity-recovered", 1)
                        self.assertIsNone(service.get_relogin_progress("worker"))

    def test_relogin_storage_failures_never_report_success_even_when_logging_fails(self):
        for operation in ("append", "rotation"):
            for failed_write in (1, 2):
                with self.subTest(operation=operation, failed_write=failed_write):
                    storage = MemoryIncrementalStorage([{
                        "access_token": "fixture-a", "status": "异常", "quota": 3,
                        "password": "fixture-password", "refresh_token": "fixture-refresh",
                    }])
                    fixture = load_isolated_service(storage)
                    service = fixture.service
                    fixture.config.auto_remove_invalid_accounts = True
                    failure = install_log_io_failure(fixture, operation)
                    fixture.namespace["sys"].stderr.write.side_effect = OSError("synthetic-stderr-secret")
                    before_accounts = deepcopy(service._accounts)
                    before_rows = deepcopy(storage.rows)
                    service._image_inflight["fixture-a"] = 2
                    service._image_cooldown_until["fixture-a"] = 50

                    def fail_selected_write():
                        storage.fail = len(storage.change_calls) + 1 == failed_write

                    storage.lock_probe = fail_selected_write
                    service._login_with_password = Mock(return_value={
                        "ok": True, "access_token": "fixture-new", "refresh_token": "fixture-new-refresh",
                    })
                    service.remove_invalid_token = Mock(wraps=service.remove_invalid_token)
                    service.update_relogin_progress = Mock(wraps=service.update_relogin_progress)
                    service.init_relogin_progress("worker", 1)
                    service._password_re_login_thread(
                        "fixture-a", "fixture@example.invalid", "fixture-password", "fixture", "worker",
                    )
                    service.remove_invalid_token.assert_not_called()
                    service.update_relogin_progress.assert_called_once_with(
                        "worker", "fixture-a", "异常", "account persistence failed",
                    )
                    progress = service.get_relogin_progress("worker")
                    self.assertTrue(progress["done"])
                    self.assertEqual(progress["processed"], 1)
                    self.assertEqual(progress["result"]["relogined"], 0)
                    self.assertEqual(len(progress["result"]["errors"]), 1)
                    self.assertEqual(len(storage.change_calls), failed_write)
                    self.assertEqual(storage.save_calls, [])
                    if failed_write == 1:
                        self.assertEqual(service._accounts, before_accounts)
                        self.assertEqual(storage.rows, before_rows)
                        self.assertEqual(service._token_aliases, {})
                        token = "fixture-a"
                    else:
                        # The first delta committed; the second must not alter memory or delete it.
                        token = "fixture-new"
                        self.assertEqual(service._accounts, storage.rows)
                        self.assertEqual(set(storage.rows), {token})
                        self.assertEqual(storage.rows[token]["status"], "异常")
                        self.assertEqual(storage.rows[token]["source_type"], "web")
                        self.assertEqual(service._token_aliases, {"fixture-a": token})
                    self.assertEqual(service._image_inflight, {token: 2})
                    self.assertEqual(service._image_cooldown_until, {token: 50})
                    self.assertNotIn("synthetic-storage-secret", repr(failure.logger.add.call_args_list))
                    self.assertNotIn("synthetic-storage-secret", repr(service._relogin_progress))

    def test_relogin_exception_cleanup_storage_failure_still_finishes_once(self):
        for operation in ("append", "rotation"):
            with self.subTest(operation=operation):
                fixture = load_isolated_service(MemoryIncrementalStorage([{"access_token": "fixture-a"}]))
                service = fixture.service
                fixture.config.auto_remove_invalid_accounts = True
                service.storage.fail = True
                before = deepcopy(service._accounts)
                install_log_io_failure(fixture, operation)
                fixture.namespace["sys"].stderr.write.side_effect = OSError("synthetic-stderr-secret")
                service._login_with_password = Mock(side_effect=RuntimeError("synthetic-auth-secret"))
                service.update_relogin_progress = Mock(wraps=service.update_relogin_progress)
                service.init_relogin_progress("worker", 1)
                with self.assertRaisesRegex(fixture.persistence_error, "^account persistence failed$"):
                    service._password_re_login_thread(
                        "fixture-a", "fixture@example.invalid", "fixture-password", "fixture", "worker",
                    )
                service.update_relogin_progress.assert_called_once_with(
                    "worker", "fixture-a", "异常", "account operation failed",
                )
                progress = service.get_relogin_progress("worker")
                self.assertTrue(progress["done"])
                self.assertEqual(progress["processed"], 1)
                self.assertEqual(progress["result"]["relogined"], 0)
                self.assertEqual(len(progress["result"]["errors"]), 1)
                self.assertEqual(service._accounts, before)
                self.assertEqual(set(service.storage.rows), {"fixture-a"})

    def test_relogin_progress_callback_error_is_not_retried_as_authentication_failure(self):
        install_log_io_failure(self.fixture, "append")
        self.service._login_with_password = Mock(return_value={"ok": True, "access_token": "fixture-new"})
        self.service.remove_invalid_token = Mock(wraps=self.service.remove_invalid_token)
        update = self.service.update_relogin_progress

        def update_then_raise(*args):
            update(*args)
            raise RuntimeError("fixture-progress-failure")

        self.service.update_relogin_progress = Mock(side_effect=update_then_raise)
        self.service.init_relogin_progress("worker", 1)
        with self.assertRaisesRegex(RuntimeError, "^fixture-progress-failure$"):
            self.service._password_re_login_thread(
                "fixture-a", "fixture@example.invalid", "fixture-password", "fixture", "worker",
            )
        self.service.remove_invalid_token.assert_not_called()
        self.service.update_relogin_progress.assert_called_once_with("worker", "fixture-a", "成功", None)
        progress = self.service.get_relogin_progress("worker")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(progress["result"]["relogined"], 1)
        self.assertEqual(self.service.get_account("fixture-new")["status"], "正常")

    def test_relogin_worker_abort_still_finishes_once_before_propagating(self):
        self.service._login_with_password = Mock(side_effect=SystemExit("fixture-abort"))
        self.service.remove_invalid_token = Mock(wraps=self.service.remove_invalid_token)
        self.service.update_relogin_progress = Mock(wraps=self.service.update_relogin_progress)
        self.service.init_relogin_progress("worker", 1)
        with self.assertRaises(SystemExit):
            self.service._password_re_login_thread(
                "fixture-a", "fixture@example.invalid", "fixture-password", "fixture", "worker",
            )
        self.service.remove_invalid_token.assert_not_called()
        self.service.update_relogin_progress.assert_called_once_with(
            "worker", "fixture-a", "异常", "account operation failed",
        )
        progress = self.service.get_relogin_progress("worker")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(progress["result"]["relogined"], 0)

    def test_relogin_thread_start_failure_is_tracked_without_authentication(self):
        self.service.update_account("fixture-a", {"email": "fixture@example.invalid"})
        self.fixture.namespace["Thread"] = Mock(side_effect=RuntimeError("fixture-thread-failure"))
        result = self.service.re_login_accounts(["fixture-a"], "thread-failure")
        self.assertEqual(result["relogined"], 0)
        self.assertEqual(len(result["errors"]), 1)
        progress = self.service.get_relogin_progress("thread-failure")
        self.assertTrue(progress["done"])
        self.assertEqual(progress["processed"], 1)
        self.assertEqual(len(progress["results"]), 1)


class FixtureHTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__("fixture HTTP error")
        self.status_code = status_code
        self.detail = detail


class FixtureRouter:
    def __init__(self):
        self.routes = {}

    def register(self, method, path):
        def decorate(function):
            self.routes[(method, path)] = function
            return function
        return decorate

    def get(self, path):
        return self.register("GET", path)

    def post(self, path):
        return self.register("POST", path)

    def delete(self, path):
        return self.register("DELETE", path)


def load_isolated_routes(fixture, create_task, run_in_threadpool):
    path = Path(__file__).resolve().parents[1] / "api" / "accounts.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = [
        node for node in tree.body
        if (isinstance(node, ast.ImportFrom) and node.module == "__future__")
        or (isinstance(node, ast.FunctionDef) and node.name in {"_unique_tokens", "create_router"})
    ]
    namespace = {
        "__name__": "isolated_account_routes_fixture",
        "__builtins__": fixture.namespace["__builtins__"],
        "APIRouter": FixtureRouter,
        "Header": lambda **kwargs: None,
        "HTTPException": FixtureHTTPException,
        "require_admin": Mock(),
        "account_service": fixture.service,
        "ProgressCapacityError": fixture.capacity_error,
        "uuid": SimpleNamespace(uuid4=lambda: "fixture-job"),
        "asyncio": SimpleNamespace(create_task=create_task),
        "run_in_threadpool": run_in_threadpool,
    }
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["create_router"]().routes


class ProgressAPIRaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_isolated_service(MemoryIncrementalStorage([
            {"access_token": "fixture-a", "quota": 2},
        ]))
        self.service = self.fixture.service
        self.pending = []

        async def run_in_threadpool(function, *args):
            return function(*args)

        self.run_in_threadpool = run_in_threadpool
        self.routes = load_isolated_routes(self.fixture, self.pending.append, run_in_threadpool)

    def tearDown(self):
        for worker in self.pending:
            worker.close()

    def post(self, kind):
        return self.routes[("POST", f"/api/accounts/{kind}")]

    def test_first_poll_exists_before_worker_runs_and_double_init_is_safe(self):
        self.service.fetch_remote_info = lambda token, *args: self.service.get_account(token)
        for path, kind in (("refresh", "refresh"), ("re-login", "relogin")):
            with self.subTest(path=path):
                body = SimpleNamespace(access_tokens=[" fixture-a ", "fixture-a"])
                response = asyncio.run(self.post(path)(body, "fixture-admin"))
                self.assertEqual(response, {"progress_id": "fixture-job"})
                poll = self.routes[("GET", f"/api/accounts/{path}/progress/{{progress_id}}")]
                progress = asyncio.run(poll("fixture-job", "fixture-admin"))
                self.assertEqual(progress["total"], 1)
                self.assertEqual(progress["processed"], 0)
                self.assertFalse(progress["done"])
                asyncio.run(self.pending.pop(0))
                progress = getattr(self.service, f"get_{kind}_progress")("fixture-job")
                self.assertTrue(progress["done"])
                self.assertEqual(progress["processed"], 1)
                self.assertEqual(progress["total"], 1)

    def test_capacity_rejected_before_any_background_task_is_created(self):
        self.service._PROGRESS_MAX_ENTRIES = 1
        self.service.init_refresh_progress("occupied", 1)
        for path in ("refresh", "re-login"):
            with self.subTest(path=path):
                with self.assertRaises(FixtureHTTPException) as caught:
                    asyncio.run(self.post(path)(SimpleNamespace(access_tokens=["fixture-a"]), "fixture-admin"))
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(self.pending, [])
                self.assertIsNone(self.service.get_refresh_progress("fixture-job"))
                self.assertIsNone(self.service.get_relogin_progress("fixture-job"))
                self.assertFalse(self.service.get_refresh_progress("occupied")["done"])

    def test_expired_poll_returns_not_found_for_both_task_types(self):
        for path, kind in (("refresh", "refresh"), ("re-login", "relogin")):
            getattr(self.service, f"init_{kind}_progress")("expired", 0)
            getattr(self.service, f"finish_{kind}_progress")("expired")
        self.fixture.clock.now = 1800
        for path in ("refresh", "re-login"):
            poll = self.routes[("GET", f"/api/accounts/{path}/progress/{{progress_id}}")]
            with self.assertRaises(FixtureHTTPException) as caught:
                asyncio.run(poll("expired", "fixture-admin"))
            self.assertEqual(caught.exception.status_code, 404)

    def test_scheduling_failure_closes_coroutine_and_finishes_reserved_record(self):
        closed = []

        def fail_schedule(worker):
            closed.append(worker)
            raise RuntimeError("fixture-scheduler-failure")

        routes = load_isolated_routes(self.fixture, fail_schedule, self.run_in_threadpool)
        for path, kind in (("refresh", "refresh"), ("re-login", "relogin")):
            with self.subTest(path=path):
                with self.assertRaises(FixtureHTTPException) as caught:
                    asyncio.run(routes[("POST", f"/api/accounts/{path}")](
                        SimpleNamespace(access_tokens=["fixture-a"]), "fixture-admin",
                    ))
                self.assertEqual(caught.exception.status_code, 503)
                self.assertIsNone(closed[-1].cr_frame)
                progress = getattr(self.service, f"get_{kind}_progress")("fixture-job")
                self.assertTrue(progress["done"])
                self.assertIsNotNone(progress["error"])

    def test_background_failure_is_recorded_without_exception_credentials(self):
        async def fail_worker(function, *args):
            raise RuntimeError("fixture-exception-credential")

        routes = load_isolated_routes(self.fixture, self.pending.append, fail_worker)
        for path, kind in (("refresh", "refresh"), ("re-login", "relogin")):
            asyncio.run(routes[("POST", f"/api/accounts/{path}")](
                SimpleNamespace(access_tokens=["fixture-a"]), "fixture-admin",
            ))
            asyncio.run(self.pending.pop(0))
            progress = getattr(self.service, f"get_{kind}_progress")("fixture-job")
            self.assertTrue(progress["done"])
            self.assertIsNotNone(progress["error"])
            self.assertNotIn("fixture-exception-credential", repr(progress))


if __name__ == "__main__":
    unittest.main()
