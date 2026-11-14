from __future__ import annotations

import ast
import hashlib
import hmac
import traceback
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from services.r2_client import CloudflareR2Client, R2Error
from curl_cffi import requests


ACCOUNT_ID = "a" * 32
SETTINGS = {
    "account_id": ACCOUNT_ID,
    "access_key_id": "synthetic-access-key",
    "secret_access_key": "synthetic-signing-secret",
    "bucket": "synthetic-images",
}


def response(status=200, *, content=b"", headers=None):
    return mock.Mock(status_code=status, content=content, headers=headers or {})


class R2ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("services.r2_client.requests.Session")
        self.session_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.session = self.session_factory.return_value
        self.client = CloudflareR2Client(SETTINGS)
        self.addCleanup(self.client.close)

    def test_default_backup_prefix_tls_and_close(self):
        self.assertEqual(self.client.prefix, "backups")
        self.session_factory.assert_called_once_with(verify=True, trust_env=False)
        self.client.close()
        self.session.close.assert_called_once_with()

    def test_backup_exports_the_same_client_and_error_without_loading_config(self):
        # Inspect the import, rather than importing the backup/config singletons.
        source = Path(__file__).resolve().parents[1] / "services" / "backup_service.py"
        module = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            entry.asname or entry.name: entry.name
            for node in module.body if isinstance(node, ast.ImportFrom) and node.module == "services.r2_client"
            for entry in node.names
        }
        self.assertEqual(names, {"CloudflareR2Client": "CloudflareR2Client", "BackupError": "R2Error"})
        self.assertFalse(any(isinstance(node, ast.ClassDef) and node.name in names for node in module.body))
        self.assertTrue(issubclass(R2Error, RuntimeError))

    def test_upload_is_one_signed_put_including_cache_control_and_metadata(self):
        self.session.request.return_value = response(headers={"etag": '"synthetic-etag"'})
        payload = b"synthetic-image-content"
        key = "images/2026/07/20/a b+image.png"
        cache = "public, max-age=0, s-maxage=60, must-revalidate"
        now = datetime(2026, 7, 20, 12, tzinfo=UTC)
        with mock.patch("services.r2_client._utc_now", return_value=now):
            result = self.client.upload_bytes(
                key, payload, content_type="image/png", metadata={"kind": "fixture"}, cache_control=cache,
            )
        self.assertEqual(result, {"key": key, "etag": "synthetic-etag"})
        self.session.request.assert_called_once()
        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("PUT", f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com/synthetic-images/images/2026/07/20/a%20b%2Bimage.png"))
        self.assertEqual(kwargs["data"], payload)
        self.assertFalse(kwargs["allow_redirects"])
        headers = kwargs["headers"]
        self.assertEqual(headers["cache-control"], cache)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["x-amz-meta-kind"], "fixture")
        signed_names = "cache-control;content-type;host;x-amz-content-sha256;x-amz-date;x-amz-meta-kind"
        self.assertIn("SignedHeaders=" + signed_names, headers["authorization"])
        body_hash = hashlib.sha256(payload).hexdigest()
        canonical = "\n".join([
            "PUT", "/synthetic-images/images/2026/07/20/a%20b%2Bimage.png", "",
            f"cache-control:{cache}\ncontent-type:image/png\nhost:{ACCOUNT_ID}.r2.cloudflarestorage.com\n"
            f"x-amz-content-sha256:{body_hash}\nx-amz-date:20260720T120000Z\nx-amz-meta-kind:fixture\n",
            signed_names, body_hash,
        ])
        signing_key = b"AWS4synthetic-signing-secret"
        for part in (b"20260720", b"auto", b"s3", b"aws4_request"):
            signing_key = hmac.new(signing_key, part, hashlib.sha256).digest()
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", "20260720T120000Z", "20260720/auto/s3/aws4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ])
        signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(headers["authorization"].endswith("Signature=" + signature))

    def test_backup_upload_does_not_add_cache_control(self):
        self.session.request.return_value = response()
        self.client.upload_bytes("backups/backup-fixture.tar.gz", b"fixture", content_type="application/octet-stream")
        self.assertNotIn("cache-control", self.session.request.call_args.kwargs["headers"])

    def test_connection_is_a_single_list_request(self):
        self.session.request.return_value = response()
        self.assertEqual(self.client.test_connection(), {"ok": True, "status": 200})
        self.session.request.assert_called_once()
        args = self.session.request.call_args.args
        self.assertEqual(args[0], "GET")
        self.assertTrue(args[1].endswith("?list-type=2&max-keys=1"))

    def test_download_and_get_alias(self):
        self.session.request.return_value = response(content=b"synthetic-object")
        self.assertEqual(self.client.download_bytes("images/fixture.png"), b"synthetic-object")
        self.assertEqual(self.client.get("images/fixture.png"), b"synthetic-object")
        self.assertEqual(self.session.request.call_count, 2)
        self.assertEqual(self.session.request.call_args.args[0], "GET")

    def test_delete_missing_object_is_successful(self):
        self.session.request.return_value = response(status=404)
        self.assertIsNone(self.client.delete_object("images/missing.png"))
        self.assertEqual(self.session.request.call_args.args[0], "DELETE")

    def test_transport_error_is_sanitized_and_not_retried(self):
        self.session.request.side_effect = requests.exceptions.RequestException(
            "https://sensitive.invalid/ synthetic-signing-secret synthetic-access-key",
        )
        try:
            self.client.upload_bytes("images/fixture.png", b"fixture", content_type="image/png")
        except R2Error as error:
            rendered = "".join(traceback.format_exception(error))
            self.assertNotIn("sensitive.invalid", rendered)
            self.assertNotIn("synthetic-signing-secret", rendered)
            self.assertNotIn("synthetic-access-key", rendered)
            self.assertIsNone(error.__cause__)
        else:
            self.fail("Expected a sanitized R2Error")
        self.session.request.assert_called_once()

    def test_http_errors_and_redirects_do_not_expose_response_body(self):
        for status in (302, 403, 500):
            with self.subTest(status=status):
                self.session.request.return_value = response(status=status, content=b"sensitive response")
                with self.assertRaises(R2Error) as raised:
                    self.client.download_bytes("images/fixture.png")
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("sensitive", str(raised.exception))

    def test_list_paginates_and_decodes_xml_entities(self):
        self.session.request.side_effect = [
            response(content=b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
                <Contents><Key>backups/old&amp;one.tar.gz</Key><Size>3</Size><LastModified>2026-07-19T00:00:00Z</LastModified></Contents>
                <IsTruncated>true</IsTruncated><NextContinuationToken>a +/&amp;</NextContinuationToken>
            </ListBucketResult>'''),
            response(content=b'''<ListBucketResult>
                <Contents><Key>backups/new.tar.gz</Key><Size>5</Size><LastModified>2026-07-20T00:00:00Z</LastModified></Contents>
                <IsTruncated>false</IsTruncated>
            </ListBucketResult>'''),
        ]
        items = self.client.list_objects()
        self.assertEqual([item["key"] for item in items], ["backups/new.tar.gz", "backups/old&one.tar.gz"])
        self.assertEqual(items[0]["size"], 5)
        url = self.session.request.call_args.args[1]
        self.assertIn("continuation-token=a%20%2B%2F%26", url)
        self.assertIn("prefix=backups%2F", url)
        self.assertEqual(self.session.request.call_count, 2)

    def test_truncated_list_without_progress_is_an_error(self):
        for token in ("", "<NextContinuationToken>same</NextContinuationToken>"):
            with self.subTest(token=token):
                self.session.request.return_value = response(content=(
                    "<ListBucketResult><IsTruncated>true</IsTruncated>" + token + "</ListBucketResult>"
                ).encode())
                with self.assertRaisesRegex(R2Error, "分页不完整"):
                    self.client.list_objects()

    def test_missing_credentials_and_bad_account_do_not_send(self):
        for settings in ({}, {**SETTINGS, "account_id": "bad.invalid/path"}):
            client = CloudflareR2Client(settings)
            with self.assertRaises(R2Error):
                client.test_connection()
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
