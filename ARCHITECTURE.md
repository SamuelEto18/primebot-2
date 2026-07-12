# PrimeBot Architecture

PrimeBot

├── Core
│
│ ├── parser.py
│
│ ├── executor.py
│
│ ├── synchronizer.py
│
│ ├── signal_processor.py
│
│ ├── duplicate_detector.py
│
│ ├── position_manager.py
│
│ ├── trade_storage.py
│
│ ├── runtime.py
│
│ ├── notifier.py
│
│ └── logger.py
│
├── Telegram
│
│ ├── Listener
│
│ ├── Command Handler
│
│ ├── Notifications
│
│ └── Keyboard
│
├── MT5
│
│ ├── Connection
│
│ ├── Orders
│
│ ├── Positions
│
│ └── Account
│
├── Storage
│
│ ├── Active Trades
│
│ ├── Runtime
│
│ ├── History
│
│ └── Processed Messages
│
├── Dashboard
│
│ ├── Status
│
│ ├── Health
│
│ ├── Balance
│
│ ├── Positions
│
│ └── Reports
│
└── Watchdog
│
├── Health Checks
│
├── Recovery
│
└── Auto Reconnect