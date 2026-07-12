import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from core.control_bot_lifecycle import ControlBotLifecycle
from core.control_center import (
    format_balance,
    format_health,
    format_ping,
    format_positions,
    format_status,
)
from core.logger import logger
from core.runtime import (
    mark_control_bot_error,
    mark_control_bot_heartbeat,
    mark_control_bot_started,
    mark_control_bot_stopped,
    pause_bot,
    resume_bot,
    set_auto_execute,
)
from core.settings import load_settings
from core.statistics import StatisticsUnavailable, generate_report_messages

KEYBOARD_ACTIONS = {
    "status": "Status",
    "health": "Health",
    "balance": "Balance",
    "positions": "Positions",
    "pause": "Pause",
    "resume": "Resume",
    "enable": "Enable",
    "disable": "Disable",
    "ping": "Ping",
}


def authorized(update):
    settings = load_settings(validate=False)

    if settings.admin_id is None:
        return False

    return update.effective_user.id == settings.admin_id


def control_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="status"),
            InlineKeyboardButton("Health", callback_data="health"),
            InlineKeyboardButton("Ping", callback_data="ping"),
        ],
        [
            InlineKeyboardButton("Balance", callback_data="balance"),
            InlineKeyboardButton("Positions", callback_data="positions"),
        ],
        [
            InlineKeyboardButton("Pause", callback_data="pause"),
            InlineKeyboardButton("Resume", callback_data="resume"),
        ],
        [
            InlineKeyboardButton("Enable", callback_data="enable"),
            InlineKeyboardButton("Disable", callback_data="disable"),
        ],
    ])


def _message_for_action(action, started_at=None):

    if action == "status":
        return format_status()

    if action == "health":
        return format_health()

    if action == "balance":
        return format_balance()

    if action == "positions":
        return format_positions()

    if action == "ping":
        return format_ping(started_at or time.perf_counter())

    if action == "pause":
        pause_bot()
        return "Trading paused."

    if action == "resume":
        resume_bot()
        return "Trading resumed."

    if action == "enable":
        set_auto_execute(True)
        return "Live trading enabled."

    if action == "disable":
        set_auto_execute(False)
        return "Dry Run enabled."

    return "Unknown Control Center action."


async def _reply(update: Update, text):

    await update.message.reply_text(
        text,
        reply_markup=control_keyboard()
    )


def _stats_preview_requested(context):
    args = getattr(context, "args", None) or []
    return bool(args) and str(args[0]).lower() == "current"


async def _reply_messages(update: Update, messages):
    for index, message in enumerate(messages):
        await update.message.reply_text(
            message,
            reply_markup=control_keyboard() if index == len(messages) - 1 else None,
        )


async def _run_command(update: Update, action):

    if not authorized(update):
        return

    started_at = time.perf_counter()
    mark_control_bot_heartbeat()

    await _reply(
        update,
        _message_for_action(action, started_at)
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not authorized(update):
        return

    mark_control_bot_heartbeat()

    await _reply(
        update,
        "PrimeBot Control Center"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "status")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "health")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "ping")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "balance")


async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "positions")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "pause")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "resume")


async def enable(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "enable")


async def disable(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await _run_command(update, "disable")


async def _run_statistics_command(update: Update, context, report_type):
    if not authorized(update):
        return

    mark_control_bot_heartbeat()
    current = _stats_preview_requested(context)

    try:
        messages = generate_report_messages(report_type, current=current)
    except StatisticsUnavailable as exc:
        await _reply(
            update,
            f"{report_type.title()} statistics unavailable.\n\n{exc}",
        )
        return
    except Exception as exc:
        logger.exception(f"{report_type.title()} statistics command failed")
        await _reply(
            update,
            f"{report_type.title()} statistics failed.\n\n{exc}",
        )
        return

    await _reply_messages(update, messages)


async def weeklystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_statistics_command(update, context, "weekly")


async def monthlystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_statistics_command(update, context, "monthly")


async def keyboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if query is not None:
        await query.answer()

    if not authorized(update):
        return

    if query is None:
        return

    mark_control_bot_heartbeat()

    started_at = time.perf_counter()
    action = query.data

    if action not in KEYBOARD_ACTIONS:
        await query.edit_message_text(
            "Unknown Control Center action.",
            reply_markup=control_keyboard()
        )
        return

    await query.edit_message_text(
        _message_for_action(action, started_at),
        reply_markup=control_keyboard()
    )


def build_control_bot_application():

    settings = load_settings(validate=True)
    app = ApplicationBuilder().token(settings.bot_token).build()

    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("positions", positions))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("enable", enable))
    app.add_handler(CommandHandler("disable", disable))
    app.add_handler(CommandHandler("weeklystats", weeklystats))
    app.add_handler(CommandHandler("monthlystats", monthlystats))
    app.add_handler(CallbackQueryHandler(keyboard_callback))

    return app


control_bot = ControlBotLifecycle(
    application_factory=build_control_bot_application,
    logger=logger,
    on_started=mark_control_bot_started,
    on_stopped=mark_control_bot_stopped,
    on_error=mark_control_bot_error,
    on_heartbeat=mark_control_bot_heartbeat,
)


def start_command_bot():

    logger.info("Starting Telegram command bot lifecycle...")

    if not control_bot.start():
        raise RuntimeError(control_bot.last_error or "Command bot failed to start")

    logger.info("Telegram command bot is running.")


def stop_command_bot():

    logger.info("Stopping Telegram command bot lifecycle...")

    return control_bot.stop()


def restart_command_bot():

    logger.info("Restarting Telegram command bot lifecycle...")

    return control_bot.restart()


def is_command_bot_running():

    return control_bot.is_running()


def get_command_bot_status():

    return control_bot.get_status()
