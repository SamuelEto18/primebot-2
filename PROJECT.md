# PrimeBot v1.0

## Overview

PrimeBot is a professional Telegram-to-MetaTrader 5 trading automation platform.

The bot listens to a private Telegram VIP channel, parses trading signals, executes MT5 trades, synchronizes edited signals, manages positions automatically, and provides a Telegram Control Center for monitoring and administration.

The goal is reliability, safety, and maintainability.

---

# Current Broker

PU Prime

MetaTrader 5

---

# Signal Source

Telegram

Using Telethon.

Only ONE VIP channel is currently supported.

Only TEXT messages are processed.

Images, GIFs, stickers, videos and documents are ignored.

---

# Trading Rules

Every TP becomes its own MT5 position.

Example:

Signal

BUY XAUUSD

TP1
TP2
TP3

Result

3 MT5 positions

Each position has:

- same entry
- same SL
- different TP

---

# Break Even

After TP1 closes

Remaining positions automatically move their Stop Loss to Entry.

Only once.

---

# Edited Signals

If the Telegram message is edited

The bot updates:

- Stop Loss
- Take Profits

without opening new trades.

---

# Runtime Modes

Dry Run

Trades are simulated.

Live

Trades are executed.

Runtime switching must be possible without restarting the bot.

---

# Control Center

Telegram Bot

Commands

/status
/health
/balance
/positions
/ping
/pause
/resume
/enable
/disable

Future

/restart
/logs
/runtime
/version

---

# Notifications

Startup

Shutdown

Dry Run

Live Trade

Break Even

Edited Signal

Errors

Warnings

Watchdog Alerts

---

# Watchdog

Monitor

Telegram Listener

Telegram Control Bot

MT5 Connection

Position Manager

Runtime

Reconnect automatically when possible.

Pause trading if MT5 is unavailable.

---

# Coding Rules

Never remove existing functionality.

Never change parser behaviour unless explicitly requested.

Never change execution behaviour unless explicitly requested.

Prefer modular code.

Keep responsibilities separated.

All new features must be backward compatible.

---

# Goal

PrimeBot v1.0 before Monday.

Stable Dry Run.

After Monday

PrimeBot Pro.