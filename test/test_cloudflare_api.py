from __future__ import annotations

import traceback
import unittest
from unittest import mock

from curl_cffi import requests

from services.cloudflare_api import CloudflareAPI, CloudflareAPIError


ACCOUNT_ID = "a" * 32
OTHER_ACCOUNT = "b" * 32
ZONE_ID = "c" * 32
START = "2026-07-01T00:00:00Z"
END = "2026-07-20T12:00:00Z"


def rest_response(result, *, info=None, status=200):
    payload = {"success": True, "errors": [], "result": result}
    if info is not None:
        payload["result_info"] = info
    return mock.Mock(status_code=status, json=mock.Mock(return_value=payload))


def operation(action, count, bucket="images", status="success"):
    return {
        "dimensions": {"actionType": action, "actionStatus": status, "bucketName": bucket},
        "sum": {"requests": count},
    }


def storage(bucket="images", *, at="2026-07-20T11:00:00Z", payload=100, metadata=5, objects=2):
    return {
        "dimensions": {"datetime": at, "bucketName": bucket},
        "max": {"payloadSize": payload, "metadataSize": metadata, "objectCount": objects},
    }


def usage_data(operations=None, snapshots=None):
    return {"viewer": {"accounts": [{
        "r2OperationsAdaptiveGroups": operations if operations is not None else [],
        "r2StorageAdaptiveGroups": snapshots if snapshots is not None else [storage()],
    }]}}


class CloudflareAPITests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("services.cloudflare_api.requests.Session")
        self.session_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self.session_factory.return_value
        self.client = CloudflareAPI(ACCOUNT_ID, "synthetic-management-token")
        self.addCleanup(self.client.close)

    def test_rest_envelope_tls_and_no_redirects(self):
        self.session.request.return_value = rest_response({"id": "synthetic-rule"})
        result = self.client.request("patch", "/zones/fixture/rulesets/ruleset/rules/rule", json={"enabled": False})
        self.assertEqual(result, {"id": "synthetic-rule"})
        self.session_factory.assert_called_once_with(verify=True, trust_env=False)
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("PATCH", "https://api.cloudflare.com/client/v4/zones/fixture/rulesets/ruleset/rules/rule"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-management-token")
        self.assertEqual(kwargs["json"], {"enabled": False})
        self.assertFalse(kwargs["allow_redirects"])
        self.client.close()
        self.session.close.assert_called_once_with()

    def test_only_api_relative_paths_are_allowed(self):
        for path in ("https://other.invalid/path", "//other.invalid/path", "/zones?token=fixture", "/zones#fragment"):
            with self.subTest(path=path), self.assertRaises(CloudflareAPIError):
                self.client.request("GET", path)
        self.session.request.assert_not_called()

    def test_transport_error_is_sanitized_without_retry(self):
        self.session.request.side_effect = requests.exceptions.RequestException(
            "https://private.invalid/ synthetic-management-token",
        )
        try:
            self.client.request("GET", "/zones")
        except CloudflareAPIError as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn("private.invalid", rendered)
            self.assertNotIn("synthetic-management-token", rendered)
            self.assertIsNone(error.__cause__)
        else:
            self.fail("Expected sanitized CloudflareAPIError")
        self.session.request.assert_called_once()

    def test_rest_errors_expose_only_status_and_numeric_codes(self):
        self.session.request.return_value = mock.Mock(status_code=403, json=mock.Mock(return_value={
            "success": False,
            "errors": [{"code": 10000, "message": "synthetic-management-token"}, {"code": "private-text"}],
        }))
        with self.assertRaises(CloudflareAPIError) as raised:
            self.client.request("GET", "/zones")
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.codes, (10000,))
        self.assertIn("10000", str(raised.exception))
        self.assertNotIn("synthetic-management-token", str(raised.exception))
        self.assertNotIn("private-text", str(raised.exception))

    def test_invalid_json_and_redirects_fail_without_body_leakage(self):
        for status in (200, 302, 503):
            with self.subTest(status=status):
                self.session.request.return_value = mock.Mock(
                    status_code=status, json=mock.Mock(side_effect=ValueError("sensitive response")),
                )
                with self.assertRaises(CloudflareAPIError) as raised:
                    self.client.request("GET", "/zones")
                self.assertNotIn("sensitive response", str(raised.exception))
                self.assertEqual(raised.exception.status_code, status)

    def test_graphql_uses_data_envelope_and_rejects_partial_errors(self):
        self.session.request.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value={"data": {"viewer": {}}}))
        self.assertEqual(self.client.graphql("query Fixture { viewer {} }", {"x": "fixture"}), {"viewer": {}})
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://api.cloudflare.com/client/v4/graphql"))
        self.assertEqual(kwargs["json"]["variables"], {"x": "fixture"})
        self.session.request.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value={
            "data": {"viewer": {}},
            "errors": [{"message": "sensitive detail", "extensions": {"code": "1020"}}],
        }))
        with self.assertRaises(CloudflareAPIError) as raised:
            self.client.graphql("query Fixture { viewer {} }", {})
        self.assertEqual(raised.exception.codes, (1020,))
        self.assertNotIn("sensitive detail", str(raised.exception))

    def test_zones_paginate_and_filter_account(self):
        wanted = {"id": ZONE_ID, "name": "images.example.test", "account": {"id": ACCOUNT_ID}}
        second = {"id": "d" * 32, "name": "backup.example.test", "account": {"id": ACCOUNT_ID}}
        other = {"id": "e" * 32, "name": "other.example.test", "account": {"id": OTHER_ACCOUNT}}
        self.session.request.side_effect = [
            rest_response([wanted, other], info={"page": 1, "total_pages": 2}),
            rest_response([second], info={"page": 2, "total_pages": 2}),
        ]
        self.assertEqual(self.client.list_zones(), [wanted, second])
        calls = self.session.request.call_args_list
        self.assertEqual([call.kwargs["params"]["page"] for call in calls], [1, 2])
        self.assertTrue(all(call.kwargs["params"]["account.id"] == ACCOUNT_ID for call in calls))

    def test_zones_do_not_silently_ignore_missing_or_unbounded_pagination(self):
        for info in (None, {"page": 1, "total_pages": 1001}, {"page": 2, "total_pages": 2}):
            with self.subTest(info=info):
                self.session.request.return_value = rest_response([], info=info)
                with self.assertRaises(CloudflareAPIError):
                    self.client.list_zones()

    def test_domain_and_managed_domain_rest_methods_preserve_cf_keys(self):
        domain = {"domain": "images.example.test", "zoneId": ZONE_ID, "enabled": False,
                  "status": {"ownership": "active", "ssl": "active"}}
        self.session.request.return_value = rest_response({"domains": [domain]})
        self.assertEqual(self.client.list_domains("images"), [domain])
        self.session.request.return_value = rest_response(domain)
        self.assertEqual(self.client.add_domain("images", domain["domain"], ZONE_ID), domain)
        self.assertEqual(self.session.request.call_args.args[0], "POST")
        self.assertEqual(self.session.request.call_args.kwargs["json"], {
            "domain": domain["domain"], "zoneId": ZONE_ID, "enabled": False,
        })
        self.client.update_domain("images", domain["domain"], True)
        self.assertEqual(self.session.request.call_args.args[0], "PUT")
        self.assertTrue(self.session.request.call_args.args[1].endswith("/domains/custom/images.example.test"))
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"enabled": True})
        self.client.delete_domain("images", domain["domain"])
        self.assertEqual(self.session.request.call_args.args[0], "DELETE")
        self.session.request.return_value = rest_response({"enabled": False, "domain": "fixture.r2.dev"})
        self.assertFalse(self.client.get_managed_domain("images")["enabled"])
        self.assertEqual(self.session.request.call_args.args[0], "GET")
        self.client.set_managed_domain("images", False)
        self.assertEqual(self.session.request.call_args.args[0], "PUT")
        self.assertTrue(self.session.request.call_args.args[1].endswith("/domains/managed"))
        self.assertEqual(self.session.request.call_args.kwargs["json"], {"enabled": False})

    def test_cors_allows_read_headers_without_credentials(self):
        self.session.request.return_value = rest_response({})
        self.client.set_cors("images", ["https://app.example.test"])
        args, kwargs = self.session.request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertTrue(args[1].endswith("/r2/buckets/images/cors"))
        self.assertEqual(kwargs["json"], {"rules": [{
            "allowed": {"origins": ["https://app.example.test"], "methods": ["GET", "HEAD"],
                        "headers": ["range", "content-type"]},
            "exposeHeaders": ["ETag", "Content-Length"],
        }]})

    def test_usage_classes_account_totals_and_latest_per_bucket(self):
        data = usage_data([
            operation("PutObject", 3), operation("ListObjects", 4, "backups"),
            operation("GetObject", 5), operation("HeadObject", 7, "backups"),
            operation("GetObject", 2, status="userError"),
            operation("GetObject", 99, status="internalError"), operation("DeleteObject", 100),
        ], [
            storage(at="2026-07-20T11:00:00Z", payload=100, metadata=5, objects=2),
            # Old image bucket peak must not be used as its current usage.
            storage(at="2026-07-20T09:00:00Z", payload=900, metadata=50, objects=10),
            # Different buckets need not share the latest timestamp.
            storage("backups", at="2026-07-20T10:00:00Z", payload=200, metadata=7, objects=3),
            storage("backups", at="2026-07-20T08:00:00Z", payload=700, metadata=9, objects=5),
        ])
        self.session.request.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value={"data": data}))
        result = self.client.query_usage(START, END, "images")
        self.assertEqual(result, {
            "a_operations": 7, "b_operations": 14, "bucket_a_operations": 3, "bucket_b_operations": 7,
            "storage_bytes": 312, "bucket_storage_bytes": 105, "object_count": 5,
            "bucket_storage_observed_at": "2026-07-20T11:00:00Z",
            "complete": True, "period_start": START, "period_end": END, "unknown_actions": [],
        })
        body = self.session.request.call_args.kwargs["json"]
        self.assertEqual(body["variables"], {
            "accountTag": ACCOUNT_ID, "startDate": START, "endDate": END, "storageStart": "2026-07-19T12:00:00Z",
        })
        for fragment in ("accountTag: $accountTag", "limit: 10000", "sum { requests }",
                         "actionType actionStatus bucketName", "orderBy: [datetime_DESC]",
                         "max { objectCount payloadSize metadataSize }"):
            self.assertIn(fragment, body["query"])

    def test_unknown_action_or_status_is_incomplete_not_zero_success(self):
        with mock.patch.object(self.client, "graphql", return_value=usage_data([
            operation("NewFutureAction", 100), operation("PutObject", 4, status="newStatus"),
        ])):
            result = self.client.query_usage(START, END, "images")
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_actions"], ["NewFutureAction"])
        self.assertEqual(result["a_operations"], 4)

    def test_operation_and_storage_caps_mark_result_incomplete(self):
        for data in (
            usage_data([operation("PutObject", 1)] * 10_000),
            usage_data(snapshots=[storage()] * 10_000),
        ):
            with self.subTest(dataset=next(iter(data))), mock.patch.object(self.client, "graphql", return_value=data):
                self.assertFalse(self.client.query_usage(START, END, "images")["complete"])

    def test_missing_storage_bucket_and_out_of_window_snapshots_are_incomplete(self):
        for snapshots in ([], [storage("backups")], [storage(at="2026-07-18T00:00:00Z")]):
            with self.subTest(snapshots=snapshots), mock.patch.object(self.client, "graphql", return_value=usage_data(snapshots=snapshots)):
                result = self.client.query_usage(START, END, "images")
                self.assertFalse(result["complete"])
                self.assertEqual(result["bucket_storage_bytes"], 0)

    def test_confirmed_empty_bucket_can_have_zero_usage(self):
        with mock.patch.object(self.client, "graphql", return_value=usage_data(snapshots=[
            storage(payload=0, metadata=0, objects=0),
        ])):
            result = self.client.query_usage(START, END, "images")
        self.assertTrue(result["complete"])
        self.assertEqual(result["storage_bytes"], 0)

    def test_missing_account_datasets_and_counters_raise_not_return_zero(self):
        bad_data = [
            {}, {"viewer": {"accounts": []}},
            {"viewer": {"accounts": [{"r2OperationsAdaptiveGroups": []}]}},
            usage_data([operation("PutObject", None)]),
            usage_data(snapshots=[storage(metadata=None)]),
        ]
        for data in bad_data:
            with self.subTest(data=data), mock.patch.object(self.client, "graphql", return_value=data):
                with self.assertRaises(CloudflareAPIError):
                    self.client.query_usage(START, END, "images")

    def test_range_is_utc_and_bounded_to_31_days_before_request(self):
        for start, end in (
            (START, START), (END, START), ("2026-06-01T00:00:00Z", END),
            ("2026-07-01T00:00:00", END), ("2026-07-01T00:00:00+08:00", END),
            ("invalid-time", END),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(CloudflareAPIError):
                self.client.query_usage(start, end, "images")
        self.session.request.assert_not_called()
        with mock.patch.object(self.client, "graphql", return_value=usage_data(snapshots=[storage(at="2026-07-31T23:00:00Z")])):
            self.assertTrue(self.client.query_usage(START, "2026-08-01T00:00:00Z", "images")["complete"])


if __name__ == "__main__":
    unittest.main()
