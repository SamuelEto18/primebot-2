import importlib
import os
import unittest
from unittest.mock import patch

from config import PRIMEBOT2_TELEGRAM_CHANNEL_ID
from core.settings import ConfigurationError, load_settings


REQUIRED_ENV = {
    "BOT_TOKEN": "123456789:test-token",
    "ADMIN_ID": "1",
    "API_ID": "1000",
    "API_HASH": "hash",
    "CHANNEL_ID": str(PRIMEBOT2_TELEGRAM_CHANNEL_ID),
    "SESSION_NAME": "primebot_test",
}

PRIMEBOT_CONFIG_KEYS = tuple(REQUIRED_ENV)


def _environment_without_primebot_credentials():
    env = dict(os.environ)
    primebot_keys = {key.upper() for key in PRIMEBOT_CONFIG_KEYS}

    for key in list(env):
        if key.upper() in primebot_keys:
            env.pop(key, None)

    return env


class SettingsTests(unittest.TestCase):

    def test_missing_configuration_value(self):
        env = dict(REQUIRED_ENV)
        env.pop("BOT_TOKEN")

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(validate=True)

        self.assertIn("BOT_TOKEN", str(ctx.exception))

    def test_invalid_integer_configuration(self):
        env = dict(REQUIRED_ENV)
        env["ADMIN_ID"] = "not-an-int"

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(validate=True)

        self.assertIn("ADMIN_ID", str(ctx.exception))

    def test_old_channel_id_is_rejected_as_configuration_mismatch(self):
        env = dict(REQUIRED_ENV)
        env["CHANNEL_ID"] = "-1002275473775"

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(validate=True)

        self.assertIn("fixed PrimeBot 2 Telegram source", str(ctx.exception))

    def test_sticker_allowlists_are_exact_integer_sets(self):
        env = dict(REQUIRED_ENV)
        env.update({
            "MT5_LOGIN": "12345678",
            "TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS": "111, 222;111",
            "TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS": "333",
            "TELEGRAM_STICKER_DISCOVERY_NOTIFY": "true",
        })

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                settings = load_settings(validate=True)

        self.assertEqual(settings.mt5_login, 12345678)
        self.assertEqual(settings.sticker_break_even_document_ids, {111, 222})
        self.assertEqual(settings.sticker_close_all_document_ids, {333})
        self.assertTrue(settings.sticker_discovery_notify)

    def test_empty_sticker_allowlists_are_disabled(self):
        env = dict(REQUIRED_ENV)
        env["TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS"] = ""
        env["TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS"] = ""

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                settings = load_settings(validate=True)

        self.assertFalse(settings.sticker_break_even_document_ids)
        self.assertFalse(settings.sticker_close_all_document_ids)

    def test_overlapping_sticker_allowlists_stop_configuration(self):
        env = dict(REQUIRED_ENV)
        env["TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS"] = "111,222"
        env["TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS"] = "222,333"

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                with self.assertRaises(ConfigurationError) as ctx:
                    load_settings(validate=True)

        self.assertIn("cannot enable both", str(ctx.exception))

    def test_missing_configuration_ignores_external_dotenv_source(self):
        env = dict(REQUIRED_ENV)
        env.pop("BOT_TOKEN")

        def dotenv_source_that_would_fill_missing_value(*args, **kwargs):
            os.environ["BOT_TOKEN"] = "123456789:from-dotenv"
            return True

        with patch.dict(os.environ, env, clear=True):
            with patch(
                "dotenv.load_dotenv",
                side_effect=dotenv_source_that_would_fill_missing_value,
            ) as external_dotenv:
                with patch("core.settings.load_dotenv") as settings_dotenv:
                    with self.assertRaises(ConfigurationError) as ctx:
                        load_settings(validate=True)

        self.assertIn("BOT_TOKEN", str(ctx.exception))
        settings_dotenv.assert_called_once()
        external_dotenv.assert_not_called()

    def test_modules_import_without_credentials(self):
        env = _environment_without_primebot_credentials()

        for key in PRIMEBOT_CONFIG_KEYS:
            self.assertNotIn(key, env)

        normalized_env = {key.upper(): value for key, value in env.items()}
        normalized_original_env = {
            key.upper(): value for key, value in os.environ.items()
        }

        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
            if key in normalized_original_env:
                self.assertEqual(
                    normalized_env.get(key), normalized_original_env[key]
                )

        with patch.dict(os.environ, env, clear=True):
            with patch("core.settings.load_dotenv"):
                import core.command_handler as command_handler
                import core.listener as listener
                import core.telegram_control as telegram_control

                importlib.reload(command_handler)
                importlib.reload(listener)
                importlib.reload(telegram_control)

                for key in PRIMEBOT_CONFIG_KEYS:
                    self.assertNotIn(key, os.environ)


if __name__ == "__main__":
    unittest.main()
