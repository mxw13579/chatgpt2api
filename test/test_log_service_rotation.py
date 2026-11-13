"""Synthetic local-file tests for the source-copy runner in test/run_isolated.py.

Run only in its temporary source tree with synthetic config/data and networking
disabled; normal imports below initialize application services.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event
from unittest import mock

from api import system
from services.log_service import LogService


def _record(summary, *, id=None, type="call", day="2026-01-02", detail=None):
    item = {
        "time": day + " 12:00:00", "type": type, "summary": summary,
        "detail": detail if detail is not None else {"synthetic": True},
    }
    if id is not None:
        item["id"] = id
    return item


def _line(item, ending=b"\n"):
    return json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + ending


class _MeasuredReader:
    def __init__(self, file, read_limit, readline_limit):
        self.file = file
        self.read_limit = read_limit
        self.readline_limit = readline_limit
        self.bytes_read = 0
        self.requests = []

    def __getattr__(self, name):
        return getattr(self.file, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()
        return False

    def __iter__(self):
        raise AssertionError("Unbounded file iteration is forbidden")

    def read(self, size=-1):
        if not 0 <= size <= self.read_limit:
            raise AssertionError("Unbounded/oversized read")
        self.requests.append(("read", self.file.tell(), size))
        data = self.file.read(size)
        self.bytes_read += len(data)
        return data

    def readline(self, size=-1):
        if not 0 < size <= self.readline_limit:
            raise AssertionError("Unbounded/oversized readline")
        self.requests.append(("readline", self.file.tell(), size))
        data = self.file.readline(size)
        self.bytes_read += len(data)
        return data


@contextmanager
def _measure_reads(directory, *, read_limit=65536, readline_limit=1048610):
    original_open = Path.open
    readers = []

    def tracked_open(path, mode="r", *args, **kwargs):
        file = original_open(path, mode, *args, **kwargs)
        if mode == "rb" and path.parent == directory:
            file = _MeasuredReader(file, read_limit, readline_limit)
            readers.append(file)
        return file

    with mock.patch.object(Path, "open", new=tracked_open):
        yield readers


class LogServiceRotationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-log-rotation-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.path = self.directory / "logs.jsonl"

    def backup(self, number):
        return self.path.with_name(f"{self.path.name}.{number}")

    def test_defaults_limit_and_missing_files(self):
        service = LogService(self.path)
        self.assertEqual(service.max_bytes, 16 * 1024 * 1024)
        self.assertEqual(service.backup_count, 5)
        self.assertEqual(service.list(), [])
        self.assertEqual(service.delete(["absent"]), {"removed": 0})
        self.path.write_bytes(b"".join(_line(_record(str(n), id=f"row-{n}")) for n in range(250)))
        rows = service.list()
        self.assertEqual(len(rows), 200)
        self.assertEqual([row["summary"] for row in rows], [str(n) for n in range(249, 49, -1)])
        self.assertEqual(len(service.list(limit=400)), 250)
        with mock.patch.object(Path, "open", side_effect=AssertionError("must not open")):
            self.assertEqual(service.list(limit=0), [])
            self.assertEqual(service.list(limit=-1), [])
            self.assertEqual(service.delete([]), {"removed": 0})
        for kwargs in ({"max_bytes": 0}, {"backup_count": -1}, {"block_size": 0}, {"max_line_bytes": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LogService(self.path, **kwargs)

    def test_rotation_retains_five_backups_and_whole_oversized_records(self):
        service = LogService(self.path, max_bytes=1)
        ids = {}
        for n in range(9):
            service.add("call", f"entry-{n}", {"n": n, "message": "合成数据"})
            ids[n] = service.list(limit=1)[0]["id"]
        rows = service.list()
        self.assertEqual([row["detail"]["n"] for row in rows], list(range(8, 2, -1)))
        self.assertEqual([row["id"] for row in rows], [ids[n] for n in range(8, 2, -1)])
        self.assertEqual(
            {path.name for path in self.directory.iterdir()},
            {"logs.jsonl", *(f"logs.jsonl.{n}" for n in range(1, 6))},
        )
        for path in self.directory.iterdir():
            self.assertEqual(json.loads(path.read_bytes())["detail"]["message"], "合成数据")

    def test_zero_backups_and_byte_threshold_reread_between_sequential_instances(self):
        no_backups = LogService(self.path, max_bytes=1, backup_count=0)
        no_backups.add("call", "discarded")
        no_backups.add("call", "retained")
        self.assertEqual([row["summary"] for row in no_backups.list()], ["retained"])
        self.assertFalse(self.backup(1).exists())
        self.path.unlink()

        summary = "合成😀" * 4
        size = len(_line({
            "id": "0" * 32, "time": "2026-01-02 12:00:00", "type": "call",
            "summary": summary, "detail": {},
        }))
        first = LogService(self.path, max_bytes=2 * size)
        second = LogService(self.directory / "unused" / ".." / "logs.jsonl", max_bytes=2 * size)
        first.add("call", summary)
        second.add("call", summary)
        self.assertEqual(self.path.stat().st_size, 2 * size)
        self.assertFalse(self.backup(1).exists())
        first.add("call", summary)
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(self.backup(1).stat().st_size, 2 * size)
        self.assertEqual(len(second.list()), 3)
        self.assertEqual(len({row["id"] for row in second.list()}), 3)

    def test_delete_updates_size_seen_by_another_instance(self):
        first = LogService(self.path)
        first.add("call", "same")
        size = self.path.stat().st_size
        first.max_bytes = 2 * size
        second = LogService(self.path, max_bytes=2 * size)
        second.add("call", "same")
        target = first.list()[-1]["id"]
        self.assertEqual(first.delete([target]), {"removed": 1})
        second.add("call", "same")
        self.assertEqual(self.path.stat().st_size, 2 * size)
        self.assertFalse(self.backup(1).exists())
        first.add("call", "same")
        self.assertEqual(self.backup(1).stat().st_size, 2 * size)

    def test_cross_shard_filters_do_not_assume_monotonic_dates(self):
        self.path.write_bytes(
            _line(_record("current-match", id="a", day="2026-02-02"))
            + _line(_record("other-type", id="b", type="account", day="2026-02-02"))
            + _line(_record("older-date-but-last-write", id="c", day="2025-01-01"))
            + b'{"torn":'
        )
        self.backup(1).write_bytes(
            _line(_record("boundary-start", id="d", day="2026-02-01"))
            + b"[]\nnull\n42\ninvalid\n\xff\n"
            + _line(_record("boundary-end", id="e", day="2026-02-03"), b"")
        )
        self.backup(2).write_bytes(_line(_record("old-shard-match", id="f", day="2026-02-02")))
        service = LogService(self.path, block_size=17)
        kwargs = {"type": "call", "start_date": "2026-02-01", "end_date": "2026-02-03"}
        self.assertEqual([row["id"] for row in service.list(**kwargs)], ["a", "e", "d", "f"])
        self.assertEqual([row["id"] for row in service.list(limit=2, **kwargs)], ["a", "e"])
        self.assertEqual([row["id"] for row in service.list(type="account")], ["b"])
        self.path.unlink()
        self.assertEqual([row["id"] for row in service.list(**kwargs)], ["e", "d", "f"])
        self.assertEqual(service.list(start_date="2099-01-01"), [])

    def test_utf8_and_crlf_across_every_small_block_boundary(self):
        records = [_record(f"中文😀-{n}", detail={"text": "跨块é😀\u2028仍是同一行"}) for n in range(8)]
        for ending in (b"", b"\n", b"\r\n"):
            self.path.write_bytes(b"\r\n".join(_line(item, b"") for item in records) + ending)
            for block_size in (1, 2, 3, 7, 31):
                with self.subTest(ending=ending, block_size=block_size):
                    service = LogService(self.path, block_size=block_size)
                    rows = service.list()
                    self.assertEqual(
                        [{key: value for key, value in row.items() if key != "id"} for row in rows],
                        records[::-1],
                    )
                    self.assertEqual(rows, LogService(self.path, block_size=13).list())

    def test_legacy_duplicate_ids_survive_rename_rewrite_and_new_instances(self):
        duplicate = _record("identical", detail={"nested": [None, True, {"text": "合成"}]})
        self.path.write_bytes(
            _line(duplicate) + _line(_record("explicit", id="keep-explicit"))
            + _line(duplicate, b"")
        )
        service = LogService(self.path, max_bytes=1)
        before = service.list()
        self.assertNotEqual(before[0]["id"], before[2]["id"])
        self.assertEqual(before[1]["id"], "keep-explicit")
        self.assertEqual(before, service.list())
        service.add("call", "new-1")
        service.add("call", "new-2")
        self.assertEqual(service.list()[2:], before)
        other = LogService(self.path, max_bytes=1)
        self.assertEqual(other.list()[2:], before)
        self.assertEqual(other.delete([before[2]["id"], before[2]["id"]]), {"removed": 1})
        self.assertEqual(other.list()[2:], before[:2])
        # IDs are persisted in the replaced shard, not cached in the instance.
        reloaded = LogService(self.path, max_bytes=1)
        self.assertEqual(reloaded.list()[2:], before[:2])
        self.assertEqual(reloaded.delete([before[0]["id"]]), {"removed": 1})
        self.assertEqual(reloaded.list()[2:], [before[1]])
        self.assertEqual(service.delete([before[0]["id"]]), {"removed": 0})

    def test_identical_legacy_lines_in_different_shards_are_distinct(self):
        raw = _line(_record("same-bytes"))
        self.path.write_bytes(raw)
        self.backup(1).write_bytes(raw)
        service = LogService(self.path)
        rows = service.list()
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(service.delete([rows[1]["id"]]), {"removed": 1})
        self.assertEqual(service.list(), [rows[0]])

    def test_explicit_ids_and_duplicate_explicit_rows_keep_existing_semantics(self):
        duplicate = _line(_record("explicit-duplicate", id="same-id"))
        self.path.write_bytes(duplicate + _line(_record("numeric", id=0)) + duplicate)
        service = LogService(self.path)
        self.assertEqual([row["id"] for row in service.list()], ["same-id", "0", "same-id"])
        self.assertEqual(service.delete(["same-id", "same-id"]), {"removed": 2})
        self.assertEqual(service.list()[0]["id"], "0")
        self.assertEqual(service.delete(["0"]), {"removed": 1})
        self.assertEqual(self.path.read_bytes(), b"")

    def test_append_separates_unterminated_and_torn_records(self):
        self.path.write_bytes(_line(_record("unterminated"), b""))
        service = LogService(self.path)
        old = service.list()[0]
        service.add("call", "after-valid", marker="synthetic")
        self.assertEqual(service.list()[1], old)
        self.assertEqual(service.list()[0]["detail"], {"marker": "synthetic"})
        with self.path.open("ab") as file:
            file.write(b'{"truncated":"\xf0\x9f')
        service.add("call", "after-torn")
        self.assertEqual([row["summary"] for row in service.list()], ["after-torn", "after-valid", "unterminated"])
        self.assertIn(b'{"truncated":"\xf0\x9f\n', self.path.read_bytes())

    def test_delete_preserves_all_unselected_bytes_including_long_and_bad_lines(self):
        keep = b'  { "id" : "keep", "detail" : {"huge":1e999,"nested":[null,true,3.1400]} }\r\n'
        oversized = _line(_record("long", id="too-long", detail={"payload": "x" * 8192}))
        corrupt = b'\xffbad\r\n\n[]\n'
        torn = b'{"unfinished":"\xf0\x9f'
        target = _line(_record("delete", id="target"))
        original = keep + target + oversized + corrupt + torn
        expected = keep + oversized + corrupt + torn
        self.path.write_bytes(original)
        service = LogService(self.path, block_size=23, max_line_bytes=160)
        self.assertEqual([row["id"] for row in service.list()], ["target", "keep"])
        with _measure_reads(self.directory, read_limit=23, readline_limit=194) as readers:
            self.assertEqual(service.delete(["target"]), {"removed": 1})
        self.assertTrue(readers)
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual(self.path.read_bytes(), expected)
        self.assertEqual([row["id"] for row in service.list()], ["keep"])
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_materialization_keeps_legacy_fields_and_boundary_sized_record_visible(self):
        legacy = b' {"detail":{"number":1e999,"array":[true,null,3.1400]},"summary":"legacy"} '
        self.path.write_bytes(_line({"id": "delete"}) + legacy)
        service = LogService(self.path, max_line_bytes=len(legacy), block_size=11)
        before = service.list()[0]
        self.assertEqual(service.delete(["delete"]), {"removed": 1})
        self.assertEqual(service.list(), [before])
        rewritten = self.path.read_bytes()
        self.assertIn(b'"number":1e999,"array":[true,null,3.1400]', rewritten)
        self.assertEqual(len(rewritten), len(legacy) + 32)
        self.assertTrue(rewritten.endswith(b"} "))
        self.assertEqual(service.delete([before["id"]]), {"removed": 1})

    def test_empty_legacy_object_and_empty_id_are_materialized(self):
        self.path.write_bytes(b'{}\n{"id":"","value":1}\n{"id":null}\n{"id":"target"}\n')
        service = LogService(self.path)
        before = service.list()
        self.assertEqual(len({row["id"] for row in before}), 4)
        self.assertEqual(service.delete(["target"]), {"removed": 1})
        self.assertEqual(service.list(), before[1:])
        self.assertEqual(service.delete([row["id"] for row in before[1:]]), {"removed": 3})

    def test_noop_delete_does_not_replace_or_materialize(self):
        original = _line(_record("legacy"), b"")
        self.path.write_bytes(original)
        service = LogService(self.path)
        before = service.list()
        identity = self.path.stat().st_ino
        with mock.patch.object(os, "replace", side_effect=AssertionError("no replacement expected")):
            self.assertEqual(service.delete(["absent", " "]), {"removed": 0})
        self.assertEqual(self.path.stat().st_ino, identity)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(service.list(), before)
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_tail_query_reads_only_a_small_suffix_and_closes_on_early_return(self):
        old = _line(_record("old-legacy", detail={"text": "synthetic-padding" * 4}))
        with self.path.open("wb") as file:
            for _ in range(20000):
                file.write(old)
            file.write(_line(_record("tail-one")))
            file.write(_line(_record("tail-two"), b""))
        size = self.path.stat().st_size
        with _measure_reads(self.directory, read_limit=4096) as readers:
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("whole text read")), \
                 mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole binary read")):
                service = LogService(self.path, block_size=4096)
                rows = service.list(limit=2)
        self.assertEqual([row["summary"] for row in rows], ["tail-two", "tail-one"])
        self.assertEqual(sum(reader.bytes_read for reader in readers), 4096)
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertTrue(all(offset >= size - 4096 for reader in readers for _, offset, _ in reader.requests))
        self.assertLess(4096, size // 100)
        with _measure_reads(self.directory, read_limit=4096) as readers:
            rows = service.list()
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({row["id"] for row in rows}), 200)
        self.assertLessEqual(sum(reader.bytes_read for reader in readers), 64 * 1024)
        self.assertTrue(all(reader.closed for reader in readers))

    def test_oversized_lines_are_skipped_without_parsing_chunks_as_records(self):
        self.path.write_bytes(
            _line({"id": "old"})
            + b"x" * 4096 + b'{"id":"not-a-separate-record"}\n'
            + _line({"id": "new"}) + b"y" * 4096
        )
        service = LogService(self.path, block_size=7, max_line_bytes=64)
        with mock.patch.object(service, "_parse_line", wraps=service._parse_line) as parse:
            self.assertEqual([row["id"] for row in service.list()], ["new", "old"])
        self.assertTrue(all(len(call.args[0]) <= 96 for call in parse.call_args_list))
        self.assertEqual(service.delete(["new"]), {"removed": 1})
        self.assertTrue(self.path.read_bytes().endswith(b"y" * 4096))
        self.assertEqual([row["id"] for row in service.list()], ["old"])

    def test_first_rotation_of_synthetic_98_mib_file_is_a_rename_not_a_scan(self):
        tail = _line(_record("old-tail"), b"")
        size = 98 * 1024 * 1024
        # Seek creates synthetic padding without allocating a 98 MiB Python blob.
        with self.path.open("wb") as file:
            file.write(_line(_record("old-first")))
            file.seek(size - len(tail) - 1)
            file.write(b"\n" + tail)
        service = LogService(self.path)
        before = service.list(limit=1)[0]
        identity = self.path.stat().st_ino
        with _measure_reads(self.directory, read_limit=1) as readers:
            service.add("call", "new-tail")
        self.assertEqual(sum(reader.bytes_read for reader in readers), 1)
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual(self.backup(1).stat().st_size, size)
        self.assertEqual(self.backup(1).stat().st_ino, identity)
        self.assertEqual(service.list(limit=2)[1], before)
        self.assertLess(self.path.stat().st_size, service.max_bytes)

    def test_replace_failure_keeps_original_and_cleans_temp_and_handles(self):
        original = _line(_record("remove", id="target")) + _line(_record("keep"))
        self.path.write_bytes(original)
        service = LogService(self.path)
        before = service.list()
        with _measure_reads(self.directory) as readers:
            with mock.patch.object(os, "replace", side_effect=OSError("synthetic replace failure")):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    service.delete(["target"])
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(service.list(), before)
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])

    def test_append_cannot_enter_between_delete_copy_and_atomic_replace(self):
        service = LogService(self.path)
        service.add("call", "remove")
        target = service.list()[0]["id"]
        at_replace, release_replace, append_started, append_done = Event(), Event(), Event(), Event()
        original_replace = os.replace

        def paused_replace(source, destination):
            if Path(destination) == self.path:
                at_replace.set()
                if not release_replace.wait(10):
                    raise AssertionError("synthetic replace wait timed out")
            return original_replace(source, destination)

        def append():
            append_started.set()
            service.add("call", "survives")
            append_done.set()

        with mock.patch.object(os, "replace", side_effect=paused_replace):
            with ThreadPoolExecutor(max_workers=2) as pool:
                deleting = pool.submit(service.delete, [target])
                try:
                    self.assertTrue(at_replace.wait(10))
                    acquired = service._lock.acquire(blocking=False)
                    if acquired:
                        service._lock.release()
                    self.assertFalse(acquired)
                    appending = pool.submit(append)
                    self.assertTrue(append_started.wait(10))
                    self.assertFalse(append_done.is_set())
                finally:
                    release_replace.set()
                self.assertEqual(deleting.result(timeout=10), {"removed": 1})
                appending.result(timeout=10)
        self.assertEqual([row["summary"] for row in service.list()], ["survives"])

    def test_same_instance_concurrent_append_rotation_delete_counts_exactly_once(self):
        service = LogService(self.path, max_bytes=512, backup_count=80)
        for n in range(50):
            service.add("call", f"seed-{n}")
        initial = service.list(limit=1000)
        targets = [row["id"] for row in initial if int(row["summary"].split("-")[1]) < 30]
        barrier = Barrier(5)

        def append(prefix):
            barrier.wait(timeout=10)
            for n in range(40):
                service.add("call", f"{prefix}-{n}")

        def delete():
            barrier.wait(timeout=10)
            return service.delete(targets + targets)["removed"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            writers = [pool.submit(append, "a"), pool.submit(append, "b")]
            deletions = [pool.submit(delete) for _ in range(3)]
            for future in writers:
                future.result(timeout=30)
            self.assertEqual(sum(future.result(timeout=30) for future in deletions), 30)
        rows = service.list(limit=1000)
        expected = (
            {f"seed-{n}" for n in range(30, 50)}
            | {f"a-{n}" for n in range(40)}
            | {f"b-{n}" for n in range(40)}
        )
        self.assertEqual({row["summary"] for row in rows}, expected)
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({row["id"] for row in rows}), 100)
        self.assertEqual(service.delete(targets), {"removed": 0})
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])


class LogRouteThreadpoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_delete_dispatch_through_threadpool(self):
        router = system.create_router("synthetic")
        get_logs = next(
            route.endpoint for route in router.routes
            if route.path == "/api/logs" and "GET" in route.methods
        )
        delete_logs = next(
            route.endpoint for route in router.routes
            if route.path == "/api/logs/delete" and "POST" in route.methods
        )
        with mock.patch.object(system, "log_service") as service, \
             mock.patch.object(system, "require_admin") as require_admin, \
             mock.patch.object(system, "run_in_threadpool", wraps=system.run_in_threadpool) as threadpool:
            service.list.return_value = [{"id": "synthetic"}]
            service.delete.return_value = {"removed": 1}
            result = await get_logs(
                type=" call ", start_date=" 2026-01-01 ", end_date=" 2026-01-02 ", authorization="synthetic-admin",
            )
            self.assertEqual(result, {"items": [{"id": "synthetic"}]})
            result = await delete_logs(system.LogDeleteRequest(ids=["synthetic"]), authorization="synthetic-admin")
            self.assertEqual(result, {"removed": 1})
            self.assertEqual(threadpool.await_args_list, [
                mock.call(service.list, type="call", start_date="2026-01-01", end_date="2026-01-02"),
                mock.call(service.delete, ["synthetic"]),
            ])
            service.list.assert_called_once_with(type="call", start_date="2026-01-01", end_date="2026-01-02")
            service.delete.assert_called_once_with(["synthetic"])
            self.assertEqual(require_admin.call_args_list, [
                mock.call("synthetic-admin"), mock.call("synthetic-admin"),
            ])


if __name__ == "__main__":
    unittest.main()
