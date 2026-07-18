import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telethon.tl.types import DocumentAttributeSticker

from config import COMMENT, MAGIC_NUMBER, PRIMEBOT2_TELEGRAM_CHANNEL_ID
from core import signal_processor, sticker_management, sticker_management_storage


BREAK_EVEN_DOCUMENT_ID = 1111111111111111111
CLOSE_ALL_DOCUMENT_ID = 2222222222222222222
OLD_SOURCE_CHAT_ID = -1002275473775
EXPECTED_MT5_LOGIN = 12345678

BASE_ENV = {
    "BOT_TOKEN": "123456789:test-token",
    "ADMIN_ID": "1",
    "API_ID": "1000",
    "API_HASH": "hash",
    "CHANNEL_ID": str(PRIMEBOT2_TELEGRAM_CHANNEL_ID),
    "SESSION_NAME": "primebot_test",
    "MT5_LOGIN": str(EXPECTED_MT5_LOGIN),
    "TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS": str(BREAK_EVEN_DOCUMENT_ID),
    "TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS": str(CLOSE_ALL_DOCUMENT_ID),
    "TELEGRAM_STICKER_DISCOVERY_NOTIFY": "false",
}


def live_position(
    ticket=101,
    identifier=501,
    symbol="XAUUSD.s",
    magic=MAGIC_NUMBER,
    comment=COMMENT,
    side=0,
    entry=100.0,
    sl=90.0,
    tp=120.0,
):
    return SimpleNamespace(
        ticket=ticket,
        identifier=identifier,
        symbol=symbol,
        magic=magic,
        comment=comment,
        type=side,
        price_open=entry,
        sl=sl,
        tp=tp,
        volume=0.01,
    )


def durable_trade(position, message_id=10):
    side = "BUY" if position.type == 0 else "SELL"
    return {
        "chat_id": PRIMEBOT2_TELEGRAM_CHANNEL_ID,
        "message_id": message_id,
        "symbol": position.symbol,
        "side": side,
        "positions": [
            {
                "ticket": position.ticket,
                "position_ticket": position.ticket,
                "position_identifier": position.identifier,
                "symbol": position.symbol,
                "side": side,
                "magic": position.magic,
                "comment": position.comment,
                "identity_status": "resolved",
                "closed": False,
                "sl": position.sl,
                "tp": position.tp,
            }
        ],
    }


def sticker_event(
    message_id=9001,
    document_id=BREAK_EVEN_DOCUMENT_ID,
    chat_id=PRIMEBOT2_TELEGRAM_CHANNEL_ID,
    emoji="✅",
    sticker=True,
):
    attributes = (
        [DocumentAttributeSticker(alt=emoji, stickerset=2713762944105054219)]
        if sticker
        else [SimpleNamespace(file_name="unknown.bin")]
    )
    document = SimpleNamespace(
        id=document_id,
        access_hash=3333333333333333333,
        mime_type="image/webp",
        attributes=attributes,
    )
    date = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
    message = SimpleNamespace(
        document=document,
        sender_id=77,
        date=date,
        edit_date=None,
    )
    return SimpleNamespace(
        id=message_id,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=77,
        raw_text="",
        text="",
        media=SimpleNamespace(document=document),
        document=document,
        message=message,
        date=date,
        edit_date=None,
    )


class StickerManagementTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=os.getcwd())
        self.addCleanup(self.temp_dir.cleanup)
        self.state_file = os.path.join(
            self.temp_dir.name,
            "processed_sticker_management.json",
        )
        self.env_patch = patch.dict(os.environ, BASE_ENV, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.storage_patch = patch.object(
            sticker_management_storage,
            "DATA_FILE",
            self.state_file,
        )
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.notify_patch = patch.object(
            sticker_management,
            "notify_sticker_management_event",
        )
        self.notify = self.notify_patch.start()
        self.addCleanup(self.notify_patch.stop)

    def _stack(
        self,
        positions=None,
        trades=None,
        account_login=EXPECTED_MT5_LOGIN,
        paused=False,
        live=True,
    ):
        positions = list(positions or [])
        trades = list(trades or [])
        stack = ExitStack()
        mocks = {}
        stack.enter_context(
            patch.object(
                sticker_management,
                "account_info",
                return_value=SimpleNamespace(login=account_login),
            )
        )
        mocks["positions_get"] = stack.enter_context(
            patch.object(sticker_management, "positions_get", return_value=positions)
        )
        stack.enter_context(
            patch.object(sticker_management, "load_trades", return_value=trades)
        )
        mocks["update_trade"] = stack.enter_context(
            patch.object(sticker_management, "update_trade")
        )
        stack.enter_context(
            patch.object(sticker_management, "position_type_buy", return_value=0)
        )
        stack.enter_context(
            patch.object(sticker_management, "position_type_sell", return_value=1)
        )
        stack.enter_context(
            patch.object(sticker_management, "is_paused", return_value=paused)
        )
        stack.enter_context(
            patch.object(sticker_management, "is_auto_execute", return_value=live)
        )
        mocks["break_even"] = stack.enter_context(
            patch.object(
                sticker_management,
                "apply_profitable_break_even",
                return_value={
                    "status": "moved",
                    "ticket": 101,
                    "target_sl": 101.0,
                },
            )
        )
        mocks["close_trade"] = stack.enter_context(
            patch.object(
                sticker_management,
                "close_trade",
                return_value={"success": True, "ticket": 101, "comment": "done"},
            )
        )
        return stack, mocks

    def _operation(self, message_id):
        operations = sticker_management_storage.load_operations()
        return operations[f"{PRIMEBOT2_TELEGRAM_CHANNEL_ID}:{message_id}"]

    def _seed_operation(
        self,
        command,
        positions,
        message_id,
        prepare=True,
    ):
        document_id = (
            BREAK_EVEN_DOCUMENT_ID
            if command == sticker_management.COMMAND_BREAK_EVEN
            else CLOSE_ALL_DOCUMENT_ID
        )
        receipt = sticker_management_storage.receive_operation(
            PRIMEBOT2_TELEGRAM_CHANNEL_ID,
            message_id,
            command,
            document_id,
            metadata={"message_id": message_id},
        )
        key = receipt["key"]
        self.assertTrue(sticker_management_storage.transition_operation(
            key,
            sticker_management_storage.STATUS_VALIDATED,
            validation={"mode": "live", "account_login": EXPECTED_MT5_LOGIN},
        ))

        if not prepare:
            return key, []

        snapshots = {}
        position_keys = []

        for item in positions:
            target = sticker_management.VerifiedPosition(live_position=item)
            position_key = sticker_management._position_key(
                EXPECTED_MT5_LOGIN,
                item,
                command,
            )
            snapshots[position_key] = sticker_management._position_snapshot(
                target,
                EXPECTED_MT5_LOGIN,
                command,
            )
            position_keys.append(position_key)

        self.assertTrue(sticker_management_storage.prepare_operation_positions(
            key,
            snapshots,
            discovery={
                "account_login": EXPECTED_MT5_LOGIN,
                "positions_discovered": len(positions),
                "eligible": len(positions),
                "skipped": [],
            },
        ))
        return key, position_keys

    def test_new_source_and_exact_green_document_id_trigger_break_even(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            handled = sticker_management.handle_sticker_management(sticker_event())

        self.assertTrue(handled)
        mocks["break_even"].assert_called_once_with(
            item,
            dry_run=False,
            expected_account_login=EXPECTED_MT5_LOGIN,
            expected_identifier=501,
        )
        operation = self._operation(9001)
        self.assertEqual(operation["command"], "break_even")
        self.assertEqual(operation["result"]["updated"], 1)

    def test_old_source_chat_is_rejected(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            handled = sticker_management.handle_sticker_management(
                sticker_event(chat_id=OLD_SOURCE_CHAT_ID)
            )

        self.assertTrue(handled)
        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        self.assertFalse(os.path.exists(self.state_file))

    def test_sticker_from_any_other_chat_is_rejected(self):
        stack, mocks = self._stack()

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(chat_id=-1009999999999)
            )

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()

    def test_unknown_document_id_is_ignored_even_with_same_emoji(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=BREAK_EVEN_DOCUMENT_ID + 1, emoji="✅")
            )

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        self.assertEqual(self._operation(9001)["status"], "IGNORED")

    def test_empty_allowlists_disable_all_sticker_commands(self):
        env = {
            "TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS": "",
            "TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS": "",
        }
        stack, mocks = self._stack()

        with patch.dict(os.environ, env, clear=False), stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        self.assertEqual(
            self._operation(9001)["result"]["reason"],
            "sticker_allowlists_empty",
        )

    def test_exact_orange_document_id_triggers_close_all(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=CLOSE_ALL_DOCUMENT_ID)
            )

        mocks["close_trade"].assert_called_once_with(
            101,
            expected_symbol="XAUUSD.s",
            expected_magic=MAGIC_NUMBER,
            expected_comment=COMMENT,
            expected_account_login=EXPECTED_MT5_LOGIN,
            expected_identifier=501,
        )
        self.assertEqual(self._operation(9001)["result"]["closed"], 1)

    def test_close_all_includes_other_configured_primebot2_symbol(self):
        item = live_position(symbol="BTCUSD")
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=CLOSE_ALL_DOCUMENT_ID)
            )

        self.assertEqual(mocks["close_trade"].call_count, 1)
        self.assertEqual(
            mocks["close_trade"].call_args.kwargs["expected_symbol"],
            "BTCUSD",
        )

    def test_manual_different_magic_and_wrong_symbol_positions_are_excluded(self):
        positions = [
            live_position(ticket=1, identifier=1, magic=0, comment="manual"),
            live_position(ticket=2, identifier=2, magic=123),
            live_position(ticket=3, identifier=3, symbol="EURUSD"),
        ]
        stack, mocks = self._stack(positions=positions, trades=[])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["break_even"].assert_not_called()
        result = self._operation(9001)["result"]
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["skipped"], 3)

    def test_bot_tagged_position_without_durable_record_is_conclusively_owned(self):
        item = live_position()
        stack, mocks = self._stack(positions=[item], trades=[])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["break_even"].assert_called_once()
        self.assertEqual(self._operation(9001)["result"]["updated"], 1)

    def test_wrong_mt5_account_blocks_before_position_query(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
            account_login=99999999,
        )

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        operation = self._operation(9001)
        self.assertEqual(operation["status"], "FAILED")
        self.assertIn("does not match", operation["result"]["reason"])

    def test_contradictory_durable_record_fails_closed_for_that_position(self):
        item = live_position()
        trade = durable_trade(item)
        trade["positions"][0]["magic"] = 123
        stack, mocks = self._stack(positions=[item], trades=[trade])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["break_even"].assert_not_called()
        self.assertEqual(self._operation(9001)["result"]["skipped"], 1)
        event_names = [call.args[0] for call in self.notify.call_args_list]
        self.assertIn("ownership_validation_failure", event_names)

    def test_correct_magic_with_wrong_comment_is_excluded(self):
        item = live_position(comment="AnotherBot")
        stack, mocks = self._stack(positions=[item], trades=[])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["break_even"].assert_not_called()
        self.assertEqual(self._operation(9001)["result"]["skipped"], 1)

    def test_unsupported_symbol_is_excluded_even_with_bot_tags(self):
        item = live_position(symbol="EURUSD")
        stack, mocks = self._stack(positions=[item], trades=[])

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=CLOSE_ALL_DOCUMENT_ID)
            )

        mocks["close_trade"].assert_not_called()
        self.assertEqual(self._operation(9001)["result"]["skipped"], 1)

    def test_duplicate_delivery_is_suppressed_durably(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )
        event = sticker_event(message_id=9100)

        with stack:
            sticker_management.handle_sticker_management(event)
            sticker_management.handle_sticker_management(event)

        mocks["break_even"].assert_called_once()
        event_names = [call.args[0] for call in self.notify.call_args_list]
        self.assertIn("duplicate_command_suppressed", event_names)

    def test_replay_after_restart_is_suppressed_by_file_state(self):
        item = live_position()
        event = sticker_event(message_id=9200)
        first_stack, first_mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with first_stack:
            sticker_management.handle_sticker_management(event)

        second_stack, second_mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with second_stack:
            sticker_management.handle_sticker_management(event)

        first_mocks["break_even"].assert_called_once()
        second_mocks["break_even"].assert_not_called()

    def test_crash_after_validation_before_first_action_is_resumed(self):
        item = live_position()
        self._seed_operation(
            sticker_management.COMMAND_BREAK_EVEN,
            [item],
            message_id=9600,
            prepare=False,
        )
        stack, mocks = self._stack(positions=[item], trades=[])

        with stack:
            summary = sticker_management.resume_pending_sticker_operations(
                force=True
            )

        self.assertEqual(summary["resumed"], 1)
        mocks["break_even"].assert_called_once()
        self.assertEqual(self._operation(9600)["status"], "COMPLETED")

    def test_received_live_state_is_validated_and_resumed_after_restart(self):
        item = live_position()
        receipt = sticker_management_storage.receive_operation(
            PRIMEBOT2_TELEGRAM_CHANNEL_ID,
            9607,
            sticker_management.COMMAND_BREAK_EVEN,
            BREAK_EVEN_DOCUMENT_ID,
            metadata={"received_mode": "live"},
        )
        self.assertTrue(receipt["created"])
        stack, mocks = self._stack(positions=[item], trades=[])

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["break_even"].assert_called_once()
        self.assertEqual(self._operation(9607)["status"], "COMPLETED")

    def test_received_dry_run_state_never_executes_after_restart(self):
        receipt = sticker_management_storage.receive_operation(
            PRIMEBOT2_TELEGRAM_CHANNEL_ID,
            9608,
            sticker_management.COMMAND_BREAK_EVEN,
            BREAK_EVEN_DOCUMENT_ID,
            metadata={"received_mode": "dry_run"},
        )
        self.assertTrue(receipt["created"])
        stack, mocks = self._stack(
            positions=[live_position()],
            trades=[],
            paused=True,
            live=False,
        )

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        self.assertEqual(self._operation(9608)["status"], "DRY_RUN_CONSUMED")

    def test_restart_with_in_progress_state_reconciles_all_targets(self):
        first = live_position(ticket=101, identifier=501)
        second = live_position(ticket=102, identifier=502)
        self._seed_operation(
            sticker_management.COMMAND_CLOSE_ALL,
            [first, second],
            message_id=9601,
        )
        stack, mocks = self._stack(positions=[first, second], trades=[])

        with stack:
            summary = sticker_management.resume_pending_sticker_operations(
                force=True
            )

        self.assertEqual(summary["resumed"], 1)
        self.assertEqual(mocks["close_trade"].call_count, 2)
        self.assertEqual(self._operation(9601)["status"], "COMPLETED")

    def test_crash_after_one_position_succeeds_resumes_only_unfinished_target(self):
        first = live_position(ticket=101, identifier=501)
        second = live_position(ticket=102, identifier=502)
        key, position_keys = self._seed_operation(
            sticker_management.COMMAND_CLOSE_ALL,
            [first, second],
            message_id=9602,
        )
        self.assertTrue(sticker_management_storage.record_position_outcome(
            key,
            position_keys[0],
            {"status": "CLOSED", "ticket": 101, "success": True},
        ))
        stack, mocks = self._stack(positions=[first, second], trades=[])

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["close_trade"].assert_called_once()
        self.assertEqual(mocks["close_trade"].call_args.args[0], 102)
        self.assertEqual(self._operation(9602)["result"]["closed"], 2)

    def test_restart_with_partial_failure_retries_only_failed_target(self):
        first = live_position(ticket=101, identifier=501)
        second = live_position(ticket=102, identifier=502)
        key, position_keys = self._seed_operation(
            sticker_management.COMMAND_CLOSE_ALL,
            [first, second],
            message_id=9603,
        )
        self.assertTrue(sticker_management_storage.record_position_outcome(
            key,
            position_keys[0],
            {"status": "CLOSED", "ticket": 101, "success": True},
        ))
        self.assertTrue(sticker_management_storage.record_position_outcome(
            key,
            position_keys[1],
            {"status": "FAILED", "ticket": 102, "success": False},
        ))
        self.assertTrue(sticker_management_storage.transition_operation(
            key,
            sticker_management_storage.STATUS_PARTIAL_FAILURE,
            result={"failed": 1, "closed": 1},
        ))
        stack, mocks = self._stack(positions=[first, second], trades=[])

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["close_trade"].assert_called_once()
        self.assertEqual(mocks["close_trade"].call_args.args[0], 102)
        self.assertEqual(self._operation(9603)["status"], "COMPLETED")

    def test_position_query_failure_before_snapshot_is_recoverable(self):
        item = live_position()
        failed_stack, failed_mocks = self._stack(positions=[], trades=[])
        failed_mocks["positions_get"].return_value = None

        with failed_stack:
            sticker_management.handle_sticker_management(
                sticker_event(message_id=9609)
            )

        self.assertEqual(self._operation(9609)["status"], "PARTIAL_FAILURE")
        failed_mocks["break_even"].assert_not_called()
        resume_stack, resume_mocks = self._stack(positions=[item], trades=[])

        with resume_stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        resume_mocks["break_even"].assert_called_once()
        self.assertEqual(self._operation(9609)["status"], "COMPLETED")

    def test_already_closed_position_is_successfully_reconciled(self):
        item = live_position()
        self._seed_operation(
            sticker_management.COMMAND_CLOSE_ALL,
            [item],
            message_id=9604,
        )
        stack, mocks = self._stack(positions=[], trades=[])

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["close_trade"].assert_not_called()
        operation = self._operation(9604)
        self.assertEqual(operation["status"], "COMPLETED")
        self.assertEqual(operation["result"]["already_absent"], 1)

    def test_already_protected_position_completes_reconciliation(self):
        item = live_position(sl=101.0)
        self._seed_operation(
            sticker_management.COMMAND_BREAK_EVEN,
            [item],
            message_id=9605,
        )
        stack, mocks = self._stack(positions=[item], trades=[])
        mocks["break_even"].return_value = {
            "status": "already_protected",
            "ticket": item.ticket,
            "target_sl": 101.0,
            "current_sl": 101.0,
        }

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        operation = self._operation(9605)
        self.assertEqual(operation["status"], "COMPLETED")
        self.assertEqual(operation["result"]["already_protected"], 1)

    def test_completed_position_is_not_worsened_or_repeated_on_partial_retry(self):
        first = live_position(ticket=101, identifier=501, sl=101.0)
        second = live_position(ticket=102, identifier=502, sl=101.0)
        key, position_keys = self._seed_operation(
            sticker_management.COMMAND_BREAK_EVEN,
            [first, second],
            message_id=9606,
        )
        self.assertTrue(sticker_management_storage.record_position_outcome(
            key,
            position_keys[0],
            {"status": "UPDATED", "ticket": 101, "target_sl": 101.0},
        ))
        self.assertTrue(sticker_management_storage.record_position_outcome(
            key,
            position_keys[1],
            {"status": "FAILED", "ticket": 102},
        ))
        self.assertTrue(sticker_management_storage.transition_operation(
            key,
            sticker_management_storage.STATUS_PARTIAL_FAILURE,
            result={"failed": 1, "updated": 1},
        ))
        stack, mocks = self._stack(positions=[first, second], trades=[])
        mocks["break_even"].return_value = {
            "status": "already_protected",
            "ticket": 102,
            "target_sl": 101.0,
            "current_sl": 101.0,
        }

        with stack:
            sticker_management.resume_pending_sticker_operations(force=True)

        mocks["break_even"].assert_called_once()
        self.assertEqual(mocks["break_even"].call_args.args[0].ticket, 102)
        operation = self._operation(9606)
        self.assertEqual(operation["result"]["updated"], 1)
        self.assertEqual(operation["result"]["already_protected"], 1)

    def test_edited_sticker_message_does_not_rerun_completed_operation(self):
        item = live_position()
        event = sticker_event(message_id=9300)
        event.edit_date = datetime(2026, 7, 18, 8, 5, tzinfo=timezone.utc)
        event.message.edit_date = event.edit_date
        stack, mocks = self._stack(
            positions=[item],
            trades=[durable_trade(item)],
        )

        with stack:
            asyncio.run(signal_processor.process_new_message(event))
            asyncio.run(signal_processor.process_edited_message(event))

        mocks["break_even"].assert_called_once()

    def test_partial_close_failure_does_not_block_other_positions(self):
        first = live_position(ticket=101, identifier=501)
        second = live_position(ticket=102, identifier=502)
        stack, mocks = self._stack(
            positions=[first, second],
            trades=[durable_trade(first, 10), durable_trade(second, 11)],
        )
        mocks["close_trade"].side_effect = [
            {"success": False, "ticket": 101, "comment": "broker rejected"},
            {"success": True, "ticket": 102, "comment": "done"},
        ]

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=CLOSE_ALL_DOCUMENT_ID)
            )

        self.assertEqual(mocks["close_trade"].call_count, 2)
        operation = self._operation(9001)
        self.assertEqual(operation["status"], "PARTIAL_FAILURE")
        self.assertEqual(operation["result"]["closed"], 1)
        self.assertEqual(operation["result"]["failed"], 1)

    def test_no_eligible_positions_produces_safe_summary(self):
        stack, mocks = self._stack(positions=[], trades=[])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["break_even"].assert_not_called()
        result = self._operation(9001)["result"]
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["updated"], 0)

    def test_stale_durable_position_is_not_an_operation_target(self):
        item = live_position()
        stack, mocks = self._stack(
            positions=[],
            trades=[durable_trade(item)],
        )

        with stack:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=CLOSE_ALL_DOCUMENT_ID)
            )

        mocks["close_trade"].assert_not_called()
        self.assertEqual(self._operation(9001)["result"]["already_absent"], 0)

    def test_dry_run_and_paused_modes_make_no_mt5_management_call(self):
        item = live_position()

        for message_id, paused, live in ((9400, False, False), (9401, True, True)):
            with self.subTest(paused=paused, live=live):
                stack, mocks = self._stack(
                    positions=[item],
                    trades=[durable_trade(item)],
                    paused=paused,
                    live=live,
                )

                with stack:
                    sticker_management.handle_sticker_management(
                        sticker_event(message_id=message_id)
                    )

                mocks["break_even"].assert_not_called()
                mocks["positions_get"].assert_not_called()
                self.assertEqual(
                    self._operation(message_id)["status"],
                    "DRY_RUN_CONSUMED",
                )

                live_stack, live_mocks = self._stack(
                    positions=[item],
                    trades=[],
                    paused=False,
                    live=True,
                )

                with live_stack:
                    sticker_management.handle_sticker_management(
                        sticker_event(message_id=message_id)
                    )

                live_mocks["positions_get"].assert_not_called()
                live_mocks["break_even"].assert_not_called()

    def test_new_message_copy_after_dry_run_can_execute_in_live_mode(self):
        item = live_position()
        dry_stack, dry_mocks = self._stack(
            positions=[item],
            trades=[],
            live=False,
        )

        with dry_stack:
            sticker_management.handle_sticker_management(
                sticker_event(message_id=9700)
            )

        dry_mocks["positions_get"].assert_not_called()
        live_stack, live_mocks = self._stack(
            positions=[item],
            trades=[],
            live=True,
        )

        with live_stack:
            sticker_management.handle_sticker_management(
                sticker_event(message_id=9701)
            )

        live_mocks["break_even"].assert_called_once()

    def test_non_sticker_documents_cannot_trigger_management(self):
        stack, mocks = self._stack()

        with stack:
            handled = sticker_management.handle_sticker_management(
                sticker_event(sticker=False)
            )

        self.assertFalse(handled)
        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()

    def test_corrupt_operation_state_fails_closed_and_notifies(self):
        with open(self.state_file, "w", encoding="utf-8") as handle:
            handle.write("{not valid json")

        stack, mocks = self._stack(positions=[live_position()], trades=[])

        with stack:
            sticker_management.handle_sticker_management(sticker_event())

        mocks["positions_get"].assert_not_called()
        mocks["break_even"].assert_not_called()
        event_names = [call.args[0] for call in self.notify.call_args_list]
        self.assertIn("complete_failure", event_names)

    def test_discovery_log_contains_required_safe_metadata(self):
        stack, _mocks = self._stack()

        with stack, self.assertLogs("PrimeBot2", level="INFO") as captured:
            sticker_management.handle_sticker_management(
                sticker_event(document_id=BREAK_EVEN_DOCUMENT_ID + 9)
            )

        discovery = next(
            line for line in captured.output if "Event=sticker_discovered" in line
        )
        self.assertIn("message_id=9001", discovery)
        self.assertIn(f"source_chat_id={PRIMEBOT2_TELEGRAM_CHANNEL_ID}", discovery)
        self.assertIn("sender_id=77", discovery)
        self.assertIn("access_hash=3333333333333333333", discovery)
        self.assertIn("mime_type='image/webp'", discovery)
        self.assertIn("is_static=True", discovery)

    def test_existing_text_signal_path_is_unaffected(self):
        event = SimpleNamespace(
            id=9500,
            message_id=9500,
            chat_id=PRIMEBOT2_TELEGRAM_CHANNEL_ID,
            raw_text="BUY XAUUSD\nSL 2300\nTP1 2400",
            text="BUY XAUUSD\nSL 2300\nTP1 2400",
            media=None,
            document=None,
            message=SimpleNamespace(document=None),
        )

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(signal_processor, "is_processed", return_value=False), \
            patch.object(signal_processor, "is_auto_execute", return_value=False), \
            patch.object(signal_processor, "notify_signal") as notify_signal, \
            patch.object(signal_processor, "notify_dry_run"), \
            patch.object(signal_processor, "mark_processed") as mark_processed, \
            patch.object(signal_processor, "mark_signal_received"):
            asyncio.run(signal_processor.process_new_message(event))

        notify_signal.assert_called_once()
        mark_processed.assert_called_once_with(9500)

    def test_existing_text_management_path_is_unaffected(self):
        event = SimpleNamespace(
            id=9501,
            message_id=9501,
            chat_id=PRIMEBOT2_TELEGRAM_CHANNEL_ID,
            raw_text="BE XAUUSD",
            text="BE XAUUSD",
            media=None,
            document=None,
            message=SimpleNamespace(document=None),
        )

        with patch.object(signal_processor, "is_paused", return_value=False), \
            patch.object(
                signal_processor,
                "process_management_message",
                return_value=True,
            ) as process_management:
            asyncio.run(signal_processor.process_new_message(event))

        process_management.assert_called_once_with(event)


if __name__ == "__main__":
    unittest.main()
