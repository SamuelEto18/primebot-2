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
