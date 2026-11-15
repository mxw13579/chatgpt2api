"""Synthetic configuration fixtures; import only through the isolated runner."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import config as config_module


class ConfigLoadingTests(unittest.TestCase):
    def test_config_store_ignores_directory_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            data_dir = base_dir / "data"
            config_dir = base_dir / "config.json"
            os_auth_key = "env-auth"
            config_dir.mkdir()

            with (
                mock.patch.object(config_module, "DATA_DIR", data_dir),
                mock.patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": os_auth_key}),
            ):
                store = config_module.ConfigStore(config_dir)

                self.assertEqual(store.auth_key, os_auth_key)
                self.assertEqual(store.refresh_account_interval_minute, 30)
                self.assertTrue(data_dir.is_dir())

    def test_config_store_auth_key_precedence_and_validation(self) -> None:
        cases = [
            (" synthetic-env-auth ", "synthetic-file-auth", "synthetic-env-auth"),
            ("", " synthetic-file-auth ", "synthetic-file-auth"),
            ("", None, None),
            ("", "   ", None),
            ("   ", "synthetic-file-auth", None),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            config_file = base_dir / "config.json"
            with mock.patch.object(config_module, "DATA_DIR", base_dir / "data"):
                for env_key, file_key, expected in cases:
                    with (
                        self.subTest(env_key=env_key, file_key=file_key),
                        mock.patch.dict(os.environ, {"CHATGPT2API_AUTH_KEY": env_key}),
                    ):
                        config_file.write_text(json.dumps({"auth-key": file_key}), encoding="utf-8")
                        if expected is None:
                            with self.assertRaisesRegex(ValueError, "auth-key 未设置"):
                                config_module.ConfigStore(config_file)
                        else:
                            store = config_module.ConfigStore(config_file)
                            self.assertEqual(store.auth_key, expected)

    def test_image_timeout_retry_secs_is_normalized_in_public_config(self) -> None:
        module = config_module
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(
                json.dumps({"auth-key": "test-auth", "image_timeout_retry_secs": "0"}),
                encoding="utf-8",
            )
            store = module.ConfigStore(config_file)

            self.assertEqual(store.image_timeout_retry_secs, 1)
            self.assertEqual(store.get()["image_timeout_retry_secs"], 1)

    def test_image_models_include_builtin_versions_and_normalize_custom_versions(self) -> None:
        module = config_module
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(
                json.dumps({
                    "auth-key": "test-auth",
                    "custom_image_models": [
                        " GPT-5-6 ",
                        "gpt-5-5",
                        "",
                        "custom-image-v1",
                        "gpt-5-6",
                    ],
                }),
                encoding="utf-8",
            )
            store = module.ConfigStore(config_file)

            self.assertEqual(store.custom_image_models, ["gpt-5-6", "custom-image-v1"])
            self.assertEqual(
                store.image_models,
                [
                    "gpt-image-2.6-sunburst",
                    "gpt-image-2.6-flare",
                    "gpt-image-2.5-sunburst",
                    "gpt-image-2.5-flare",
                    "gpt-image-2",
                    "gpt-5-5-thinking",
                    "gpt-5-5",
                    "gpt-5-3",
                    "gpt-5-6",
                    "custom-image-v1",
                ],
            )
            self.assertEqual(store.get()["custom_image_models"], ["gpt-5-6", "custom-image-v1"])
            self.assertEqual(
                store.get()["image_models"],
                [
                    "gpt-image-2.6-sunburst",
                    "gpt-image-2.6-flare",
                    "gpt-image-2.5-sunburst",
                    "gpt-image-2.5-flare",
                    "gpt-image-2",
                    "gpt-5-5-thinking",
                    "gpt-5-5",
                    "gpt-5-3",
                    "gpt-5-6",
                    "custom-image-v1",
                ],
            )

    def test_update_persists_refreshed_image_model_cache(self) -> None:
        module = config_module
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = module.ConfigStore(config_file)

            saved = store.update({
                "image_models_cache": ["gpt-image-2.5-sunburst", "gpt-image-2", "gpt-5-7"],
                "image_models_source": "upstream",
                "image_models_updated_at": "2026-09-20T12:00:00+00:00",
            })

            self.assertEqual(saved["image_models"], [*module.DEFAULT_IMAGE_MODELS, "gpt-5-7"])
            self.assertEqual(saved["image_models_source"], "upstream")
            self.assertEqual(saved["image_models_updated_at"], "2026-09-20T12:00:00+00:00")

    def test_new_builtin_models_can_be_selected_with_legacy_cache(self) -> None:
        module = config_module
        for model in ("gpt-image-2.6-flare", "gpt-image-2.6-sunburst"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as tmp_dir:
                config_file = Path(tmp_dir) / "config.json"
                config_file.write_text(json.dumps({
                    "auth-key": "test-auth",
                    "image_models_cache": ["gpt-image-2.5-sunburst", "gpt-image-2", "gpt-5-7"],
                    "default_image_model": "gpt-image-2.5-sunburst",
                }), encoding="utf-8")
                store = module.ConfigStore(config_file)

                self.assertIn(model, store.image_models)
                self.assertIn("gpt-5-7", store.image_models)
                self.assertEqual(store.default_image_model, "gpt-image-2.5-sunburst")
                self.assertEqual(len(store.image_models), len(set(store.image_models)))

                saved = store.update({"default_image_model": model})
                reloaded = module.ConfigStore(config_file)

                self.assertEqual(saved["default_image_model"], model)
                self.assertEqual(reloaded.default_image_model, model)
                self.assertIn(model, reloaded.get()["image_models"])

    def test_default_image_model_is_saved_and_reloaded(self) -> None:
        module = config_module
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(json.dumps({"auth-key": "test-auth"}), encoding="utf-8")
            store = module.ConfigStore(config_file)

            saved = store.update({
                "custom_image_models": ["gpt-image-2.5-sunburst"],
                "default_image_model": "gpt-image-2.5-sunburst",
            })
            reloaded = module.ConfigStore(config_file)

            self.assertEqual(saved["default_image_model"], "gpt-image-2.5-sunburst")
            self.assertEqual(reloaded.default_image_model, "gpt-image-2.5-sunburst")
            self.assertEqual(reloaded.get()["default_image_model"], "gpt-image-2.5-sunburst")

    def test_default_image_model_falls_back_when_custom_model_is_removed(self) -> None:
        module = config_module
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config.json"
            config_file.write_text(
                json.dumps({
                    "auth-key": "test-auth",
                    "custom_image_models": ["custom-image-v1"],
                    "default_image_model": "custom-image-v1",
                }),
                encoding="utf-8",
            )
            store = module.ConfigStore(config_file)

            saved = store.update({"custom_image_models": []})

            self.assertEqual(saved["default_image_model"], "gpt-image-2")


if __name__ == "__main__":
    unittest.main()
