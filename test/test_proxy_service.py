import unittest
from unittest.mock import patch

from services.proxy_service import ProxySettingsStore, normalize_proxy_url, test_proxy as run_proxy_test


class FakeConfig:
    def __init__(self, proxy: str = "") -> None:
        self.proxy = proxy

    def get_proxy_settings(self) -> str:
        return self.proxy


class ProxyServiceTests(unittest.TestCase):
    def test_normalize_proxy_url_strips_and_converts_socks_schemes(self) -> None:
        self.assertEqual(normalize_proxy_url("  http://proxy.example:8080  "), "http://proxy.example:8080")
        self.assertEqual(normalize_proxy_url("\thttps://proxy.example:8443\n"), "https://proxy.example:8443")
        self.assertEqual(normalize_proxy_url(" socks://proxy.example:1080 "), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url("socks5://proxy.example:1080"), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url(" socks5h://proxy.example:1080 "), "socks5h://proxy.example:1080")
        self.assertEqual(normalize_proxy_url("   "), "")

    def test_build_session_kwargs_uses_global_proxy(self) -> None:
        store = ProxySettingsStore(FakeConfig("  http://global.example:8080  "))

        kwargs = store.build_session_kwargs(impersonate="chrome")

        self.assertEqual(kwargs, {"impersonate": "chrome", "proxy": "http://global.example:8080"})

    def test_account_proxy_wins_over_global_proxy(self) -> None:
        store = ProxySettingsStore(FakeConfig("http://global.example:8080"))

        kwargs = store.build_session_kwargs(account={"proxy": "socks5://account.example:1080"})

        self.assertEqual(kwargs["proxy"], "socks5h://account.example:1080")

    def test_build_session_kwargs_without_proxy_leaves_kwargs_untouched(self) -> None:
        self.assertEqual(ProxySettingsStore(FakeConfig()).build_session_kwargs(verify=True), {"verify": True})

    def test_proxy_test_error_redacts_proxy_credentials(self) -> None:
        class FailingSession:
            def __init__(self, **kwargs: object) -> None:
                pass

            def get(self, *args: object, **kwargs: object) -> object:
                raise RuntimeError("proxy failed for http://user:pass@proxy.example:8080")

            def close(self) -> None:
                pass

        with patch("services.proxy_service.Session", FailingSession):
            result = run_proxy_test("http://user:pass@proxy.example:8080")

        self.assertFalse(result["ok"])
        self.assertIn("[REDACTED]", result["error"])
        self.assertNotIn("user:pass", result["error"])
