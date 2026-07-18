# PrimeBot 2 VPS deployment

Run these steps from an elevated PowerShell session on the VPS. Keep the Git
checkout separate from `C:\PrimeBot`, which remains the production directory.

1. Install Git for Windows manually from <https://git-scm.com/download/win>.
   The PrimeBot scripts never install Git.
2. Clone the existing repository directly. The bootstrap script cannot be used
   before this first clone because it does not exist on a new VPS yet:

   ```powershell
   git clone https://github.com/SamuelEto18/primebot-2.git C:\PrimeBot2-Repo
   ```

3. Validate the clone and production path without changing either one:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\PrimeBot2-Repo\scripts\Bootstrap-PrimeBot2Repo.ps1 -RepositoryPath C:\PrimeBot2-Repo -ProductionPath C:\PrimeBot -ValidationOnly
   ```

4. Deploy only an explicitly approved commit. This example remains stopped,
   paused, and in Dry Run after verification:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\PrimeBot2-Repo\scripts\Deploy-PrimeBot2.ps1 -RepositoryPath C:\PrimeBot2-Repo -ProductionPath C:\PrimeBot -ExpectedCommit 2e7d733
   ```

The deployer fetches `origin`, requires the commit to be reachable from
`origin/main`, and verifies it in a temporary detached worktree under
`C:\PrimeBot2-DeployWorktrees`. It never checks out the requested commit in
`C:\PrimeBot2-Repo`. The main clone keeps its original branch and HEAD, and the
temporary worktree is removed after both successful and failed deployments.

Use `-StartPausedDryRun` only when the verified bot should be started through
the `PrimeBot AutoStart` scheduled task. The switch never enables Live mode.

## Sticker document-ID discovery on VPS 2

Do not edit the production checkout or either VPS directly through Codex. After
an approved Git deployment to VPS 2, use this safe procedure:

1. In the protected production `.env`, set `CHANNEL_ID=-1002792547449`, keep the
   existing `MT5_LOGIN`, leave both sticker allowlists empty, and optionally set
   `TELEGRAM_STICKER_DISCOVERY_NOTIFY=true`.
2. Start PrimeBot 2 paused and in Dry Run. Empty allowlists guarantee that no
   sticker management command is enabled.
3. Post each command sticker once in source channel `-1002792547449`.
4. Read `logs\primebot.log` (or the configured `PRIMEBOT_LOG_FILE`) and locate
   `Event=sticker_discovered`. Record the `document_id` for each message. Verify
   the message ID, source chat ID, sticker emoji/set metadata, MIME type, and
   static/animated/video flags. Never copy the Telethon session file, bot token,
   API hash, or MT5 credentials.
5. Put only the confirmed green sticker ID in
   `TELEGRAM_STICKER_BREAK_EVEN_DOCUMENT_IDS` and only the confirmed orange/red
   sticker ID in `TELEGRAM_STICKER_CLOSE_ALL_DOCUMENT_IDS` (comma-separated if a
   command later has multiple approved IDs). A document ID must not appear in
   both fields.
6. Restart PrimeBot 2, confirm startup accepts the configuration, keep it paused
   and in Dry Run for an allowlist-match audit, and enable Live mode only through
   the existing approved control procedure.

Sticker management requires the connected MT5 account login to match
`MT5_LOGIN`, plus live magic `987655`, comment `PrimeBot2`, an allowed broker
symbol, and a current MT5 ticket and position identifier. A matching durable
trade record is cross-checked and any contradiction fails closed for that
position; absence of a durable record alone does not exclude an otherwise
conclusively owned position. Break-even is restricted to `XAUUSD.s`; close all
covers the configured allowed symbols.
