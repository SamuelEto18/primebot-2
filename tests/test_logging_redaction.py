import io
import logging
import os
import tempfile
import unittest

from core import logger as prime_logger


FAKE_TOKEN = "123456789:FAKE_token_FOR_TESTS"


class LoggingRedactionTests(unittest.TestCase):

    def setUp(self):
        self.original_token = os.environ.get("BOT_TOKEN")
        os.environ["BOT_TOKEN"] = FAKE_TOKEN

    def tearDown(self):
        if self.original_token is None:
            os.environ.pop("BOT_TOKEN", None)
        else:
            os.environ["BOT_TOKEN"] = self.original_token

    def make_logger(self, *handlers):
        logger = logging.getLogger(f"PrimeBotRedactionTest.{id(self)}")
        logger.handlers = []
        logger.propagate = False
        logger.setLevel(logging.INFO)

        for handler in handlers:
            handler.addFilter(prime_logger.SecretRedactionFilter())
            handler.setFormatter(prime_logger.RedactingFormatter("%(message)s"))
            logger.addHandler(handler)

        return logger

    def close_logger(self, logger):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_telegram_api_url_is_redacted(self):
        text = (
            "GET https://api.telegram.org/"
            f"bot{FAKE_TOKEN}/sendMessage failed"
        )

        redacted = prime_logger.redact_text(text)

        self.assertNotIn(FAKE_TOKEN, redacted)
        self.assertIn("bot[REDACTED]/sendMessage", redacted)

    def test_raw_fake_token_is_redacted(self):
        redacted = prime_logger.redact_text(f"token={FAKE_TOKEN}")

        self.assertEqual(redacted, "token=[REDACTED]")

    def test_parameterized_log_message_is_redacted(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = self.make_logger(handler)

        logger.info("telegram token=%s", FAKE_TOKEN)
        output = stream.getvalue()

        self.assertNotIn(FAKE_TOKEN, output)
        self.assertIn("[REDACTED]", output)

    def test_exception_text_is_redacted(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = self.make_logger(handler)

        try:
            raise RuntimeError(f"request failed for {FAKE_TOKEN}")
        except RuntimeError:
            logger.exception("notification failed")

        output = stream.getvalue()

        self.assertNotIn(FAKE_TOKEN, output)
        self.assertIn("[REDACTED]", output)

    def test_normal_non_secret_messages_remain_unchanged(self):
        message = "PrimeBot started in dry run"

        self.assertEqual(prime_logger.redact_text(message), message)

    def test_file_and_stream_output_contain_no_fake_token(self):
        stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "primebot.log")
            stream_handler = logging.StreamHandler(stream)
            file_handler = logging.FileHandler(
                log_path,
                encoding="utf-8",
                errors="backslashreplace",
            )
            logger = self.make_logger(stream_handler, file_handler)

            logger.info(
                "POST https://api.telegram.org/bot%s/sendMessage",
                FAKE_TOKEN,
            )

            for handler in logger.handlers:
                handler.flush()

            with open(log_path, "r", encoding="utf-8") as handle:
                file_output = handle.read()

            self.close_logger(logger)

        stream_output = stream.getvalue()

        self.assertNotIn(FAKE_TOKEN, stream_output)
        self.assertNotIn(FAKE_TOKEN, file_output)
        self.assertIn("[REDACTED]", stream_output)
        self.assertIn("[REDACTED]", file_output)

    def test_utf8_file_output_preserves_signal_text_and_redacts_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "primebot.log")
            file_handler = prime_logger.create_primebot_file_handler(log_path)
            logger = self.make_logger(file_handler)

            logger.info(
                "🚀 Semnal Cumpărați → XAUUSD.s\n"
                "Entry 4020,5 - 4025,5\n"
                "TP1 4010,5\n"
                "SL 4030,0\n"
                f"Token {FAKE_TOKEN}"
            )

            for handler in logger.handlers:
                handler.flush()

            with open(log_path, "r", encoding="utf-8") as handle:
                output = handle.read()

            self.close_logger(logger)

        self.assertIn("🚀", output)
        self.assertIn("Cumpărați", output)
        self.assertIn("→", output)
        self.assertIn("Entry 4020,5 - 4025,5", output)
        self.assertIn("TP1 4010,5\nSL 4030,0", output)
        self.assertNotIn(FAKE_TOKEN, output)
        self.assertIn("[REDACTED]", output)

    def test_managed_file_handlers_are_utf8_safe_and_test_isolated(self):
        production_log = os.path.abspath(prime_logger.DEFAULT_LOG_FILE)
        file_handlers = [
            handler for handler in logging.getLogger().handlers
            if (
                getattr(handler, "_primebot_managed", False)
                and isinstance(handler, logging.FileHandler)
            )
        ]

        self.assertGreaterEqual(len(file_handlers), 1)

        for handler in file_handlers:
            with self.subTest(path=handler.baseFilename):
                self.assertEqual(handler.encoding.lower().replace("-", ""), "utf8")
                self.assertEqual(handler.errors, "backslashreplace")
                self.assertNotEqual(
                    os.path.abspath(handler.baseFilename),
                    production_log,
                )

    def test_http_dependency_loggers_are_above_info(self):
        prime_logger.configure_dependency_loggers()

        self.assertGreater(
            logging.getLogger("httpx").getEffectiveLevel(),
            logging.INFO,
        )
        self.assertGreater(
            logging.getLogger("httpcore").getEffectiveLevel(),
            logging.INFO,
        )


if __name__ == "__main__":
    unittest.main()
