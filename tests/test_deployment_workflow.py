import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "Deploy-PrimeBot2.ps1"
BOOTSTRAP_SCRIPT = ROOT / "scripts" / "Bootstrap-PrimeBot2Repo.ps1"
DEPLOYMENT_MODULE = ROOT / "scripts" / "PrimeBot2.Deployment.psm1"
DEPLOYMENT_MANIFEST = ROOT / "scripts" / "PrimeBot2.DeploymentManifest.json"
DEPLOYMENT_DOC = ROOT / "scripts" / "DEPLOYMENT.md"
APPROVED_ORIGIN = "https://github.com/SamuelEto18/primebot-2.git"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


class RetryingTemporaryDirectory(tempfile.TemporaryDirectory):
    """Tolerate short-lived Windows Git/PowerShell directory handles."""

    def cleanup(self):
        last_error = None

        for _attempt in range(20):
            try:
                super().cleanup()
                return
            except OSError as error:
                last_error = error
                time.sleep(0.1)

        raise last_error


def run(command, *, cwd=None, env=None, check=True, timeout=120):
    result = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )

    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed ({result.returncode}): {' '.join(map(str, command))}\n"
            f"{result.stdout}"
        )

    return result


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def generated_test_module(fail=False):
    failing_body = "self.fail('intentional deployment test failure')" if fail else "self.assertTrue(True)"
    return f'''import unittest
from pathlib import Path


class GeneratedDeploymentTests(unittest.TestCase):
    def test_generated_000_worktree_isolated(self):
        source_root = Path(__file__).resolve().parents[1]
        self.assertEqual(Path.cwd().resolve(), source_root)
        self.assertFalse((source_root / ".env").exists())
        self.assertFalse((source_root / ".venv").exists())
        self.assertFalse((source_root / "data").exists())
        self.assertFalse((source_root / "logs").exists())
        self.assertFalse((source_root / "deployment-archives").exists())
        self.assertEqual(list(source_root.glob("*.session*")), [])


def _make_test(index):
    def test(self):
        if index == 220:
            {failing_body}
        else:
            self.assertTrue(True)
    return test


for _index in range(1, 221):
    setattr(
        GeneratedDeploymentTests,
        f"test_generated_{{_index:03d}}",
        _make_test(_index),
    )
'''


class DeploymentEnvironment:
    def __init__(
        self,
        root,
        *,
        identity_ok=True,
        failing_tests=False,
        bad_compile=False,
        mt5_server="PUPrime-Live 6",
    ):
        self.root = Path(root)
        self.repository = self.root / "repository"
        self.origin = self.root / "origin.git"
        self.production = self.root / "production"
        self.worktree_root = self.root / "worktrees"
        self.global_git_config = self.root / "gitconfig"
        self.env = os.environ.copy()
        self._create_repository(
            identity_ok=identity_ok,
            failing_tests=failing_tests,
            bad_compile=bad_compile,
        )
        self._create_production(mt5_server=mt5_server)
        self.initial_repository_state = self.repository_state()

    def _git(self, *arguments, cwd=None, check=True):
        return run(
            ["git", *arguments],
            cwd=cwd or self.repository,
            env=self.env,
            check=check,
        )

    def _create_repository(self, *, identity_ok, failing_tests, bad_compile):
        run(["git", "init", "--bare", self.origin])
        run(["git", "init", self.repository])
        self._git("branch", "-M", "main")
        self._git("config", "user.name", "PrimeBot Deployment Tests")
        self._git("config", "user.email", "deployment-tests@example.invalid")
        self._git("config", "core.autocrlf", "false")

        chat_id = "-1002275473775" if identity_ok else "-1009999999999"
        write(
            self.repository / "config.py",
            "\n".join(
                [
                    f"PRIMEBOT2_TELEGRAM_CHANNEL_ID = {chat_id}",
                    "MAGIC_NUMBER = 987655",
                    'COMMENT = "PrimeBot2"',
                    'PROFITABLE_BREAK_EVEN_SYMBOL = "XAUUSD.s"',
                    "",
                ]
            ),
        )
        write(self.repository / "main.py", "print('sample main is not started by tests')\n")
        write(self.repository / "control_bot.py", "# sample control bot\n")
        write(self.repository / "requirements.txt", "# sample requirements\n")
        write(self.repository / "core" / "__init__.py", "")
        write(self.repository / "core" / "break_even.py", "VALUE = 'break-even'\n")
        write(self.repository / "core" / "break_even_storage.py", "VALUE = 'storage'\n")
        write(self.repository / "core" / "modified.py", "VALUE = 'new tracked content'\n")
        write(self.repository / "core" / "new_file.py", "VALUE = 'new tracked file'\n")
        write(self.repository / "tests" / "test_generated.py", generated_test_module(failing_tests))

        if bad_compile:
            write(self.repository / "core" / "bad_syntax.py", "def broken(:\n")

        self._git("add", ".")
        self._git("commit", "-m", "sample approved deployment")
        self.older_commit = self._git("rev-parse", "HEAD").stdout.strip()

        tooling_directory = self.repository / "scripts"
        tooling_directory.mkdir(parents=True, exist_ok=True)

        for tooling_file in (
            DEPLOY_SCRIPT,
            BOOTSTRAP_SCRIPT,
            DEPLOYMENT_MODULE,
            DEPLOYMENT_MANIFEST,
            DEPLOYMENT_DOC,
        ):
            shutil.copy2(tooling_file, tooling_directory / tooling_file.name)
        self._git("add", ".")
        self._git("commit", "-m", "add deployment tooling after older application commit")
        self.commit = self._git("rev-parse", "HEAD").stdout.strip()

        self._git("remote", "add", "origin", APPROVED_ORIGIN)
        origin_uri = self.origin.resolve().as_uri()
        write(
            self.global_git_config,
            f'[url "{origin_uri}"]\n\tinsteadOf = {APPROVED_ORIGIN}\n',
        )
        self.env["GIT_CONFIG_GLOBAL"] = str(self.global_git_config)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        self._git("push", "-u", "origin", "HEAD:main")
        run(["git", "--git-dir", self.origin, "symbolic-ref", "HEAD", "refs/heads/main"])

    def _create_production(self, *, mt5_server):
        self.production.mkdir(parents=True)
        protected = {
            ".env": "BOT_TOKEN=keep-me\nMT5_LOGIN=12345678\n",
            "primebot.session": "session-bytes",
            "primebot.session-journal": "session-journal-bytes",
            ".venv/sentinel.txt": "preserve virtual environment",
            "data/positions.json": '{"open": [1]}',
            "logs/primebot.log": "preserve logs",
            "deployment-archives/existing/archive.zip": "preserve archive",
            "credentials/mt5.credentials": "preserve MT5 credentials",
        }

        for relative, content in protected.items():
            write(self.production / relative, content)

        base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        production_python = self.production / ".venv" / "Scripts" / "python.exe"
        production_python.parent.mkdir(parents=True, exist_ok=True)
        os.link(base_python, production_python)
        write(
            self.production / ".venv" / "pyvenv.cfg",
            "\n".join(
                [
                    f"home = {base_python.parent}",
                    "include-system-site-packages = true",
                    f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    f"executable = {base_python}",
                    "",
                ]
            ),
        )
        write(
            self.production / ".venv" / "Lib" / "site-packages" / "MetaTrader5.py",
            "\n".join(
                [
                    "from types import SimpleNamespace",
                    "",
                    "def initialize():",
                    "    return True",
                    "",
                    "def account_info():",
                    f"    return SimpleNamespace(server={mt5_server!r}, login=12345678)",
                    "",
                    "def shutdown():",
                    "    return None",
                    "",
                ]
            ),
        )

        protected[".venv/Scripts/python.exe"] = None
        protected[".venv/pyvenv.cfg"] = None
        protected[".venv/Lib/site-packages/MetaTrader5.py"] = None

        self.protected_before = {
            relative: (self.production / relative).read_bytes()
            for relative in protected
        }
        write(
            self.production / "data" / "runtime.json",
            json.dumps(
                {
                    "paused": False,
                    "auto_execute": True,
                    "preserved": "runtime-field",
                },
                indent=2,
            ),
        )
        write(self.production / "core" / "modified.py", "VALUE = 'old production content'\n")
        write(self.production / "core" / "removed.py", "VALUE = 'removed file backup'\n")
        write(self.production / "untracked-production.txt", "preserve untracked production file\n")
        write(
            self.production / ".primebot2-deployment-state.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "sourceCommit": "old",
                    "managedFiles": [
                        "core/modified.py",
                        "core/removed.py",
                        "data/positions.json",
                    ],
                },
                indent=2,
            ),
        )

    def deploy(self, expected_commit=None):
        return run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.repository / "scripts" / "Deploy-PrimeBot2.ps1",
                "-RepositoryPath",
                self.repository,
                "-ProductionPath",
                self.production,
                "-ExpectedCommit",
                expected_commit or self.commit,
                "-WorktreeRoot",
                self.worktree_root,
            ],
            env=self.env,
            check=False,
        )

    def repository_state(self):
        return {
            "branch": self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "head": self._git("rev-parse", "HEAD").stdout.strip(),
            "status": self._git(
                "status", "--porcelain", "--untracked-files=all"
            ).stdout,
        }

    def worktree_path_from_output(self, output):
        match = re.search(r"Verification working directory:\s*(.*)", output)
        self_path = match.group(1).strip() if match else ""
        return Path(self_path) if self_path else None

    def assert_worktree_removed(self, test_case, result):
        worktree_path = self.worktree_path_from_output(result.stdout)
        test_case.assertIsNotNone(worktree_path, result.stdout)
        test_case.assertFalse(worktree_path.exists(), result.stdout)
        listed = self._git("worktree", "list", "--porcelain").stdout
        test_case.assertNotIn(str(worktree_path), listed)
        if self.worktree_root.exists():
            test_case.assertEqual(list(self.worktree_root.iterdir()), [])

    def assert_protected_unchanged(self, test_case):
        for relative, expected in self.protected_before.items():
            with test_case.subTest(relative=relative):
                test_case.assertEqual((self.production / relative).read_bytes(), expected)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class SuccessfulDeploymentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = RetryingTemporaryDirectory()
        cls.environment = DeploymentEnvironment(cls.temp_dir.name)
        cls.result = cls.environment.deploy()

        if cls.result.returncode != 0:
            cls.temp_dir.cleanup()
            raise AssertionError(cls.result.stdout)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_protected_files_remain_unchanged(self):
        self.environment.assert_protected_unchanged(self)

    def test_new_tracked_file_is_deployed(self):
        content = (self.environment.production / "core" / "new_file.py").read_text(encoding="utf-8")
        self.assertIn("new tracked file", content)
        self.assertTrue((self.environment.production / "core" / "break_even.py").is_file())
        self.assertTrue((self.environment.production / "core" / "break_even_storage.py").is_file())

    def test_modified_tracked_file_is_replaced(self):
        content = (self.environment.production / "core" / "modified.py").read_text(encoding="utf-8")
        self.assertEqual(content, "VALUE = 'new tracked content'\n")

    def test_removed_managed_file_is_removed(self):
        self.assertFalse((self.environment.production / "core" / "removed.py").exists())

    def test_untracked_production_file_is_preserved(self):
        content = (self.environment.production / "untracked-production.txt").read_text(encoding="utf-8")
        self.assertEqual(content, "preserve untracked production file\n")

    def test_paused_and_dry_run_are_enforced(self):
        state = json.loads((self.environment.production / "data" / "runtime.json").read_text(encoding="utf-8-sig"))
        self.assertTrue(state["paused"])
        self.assertFalse(state["auto_execute"])
        self.assertEqual(state["preserved"], "runtime-field")
        self.assertIn("Final runtime mode: paused=True; auto_execute=False", self.result.stdout)

    def test_successful_deployment_leaves_complete_backup(self):
        match = re.search(r"Backup directory:\s*(.+)", self.result.stdout)
        self.assertIsNotNone(match, self.result.stdout)
        backup = Path(match.group(1).strip())
        index = json.loads((backup / "backup-index.json").read_text(encoding="utf-8-sig"))
        records = {item["relativePath"]: item for item in index["records"]}
        self.assertIn("core/modified.py", records)
        self.assertIn("core/removed.py", records)
        self.assertEqual(
            (backup / "files" / "core" / "modified.py").read_text(encoding="utf-8"),
            "VALUE = 'old production content'\n",
        )
        self.assertEqual(
            (backup / "files" / "core" / "removed.py").read_text(encoding="utf-8"),
            "VALUE = 'removed file backup'\n",
        )
        self.assertTrue((backup / "protected" / "data" / "runtime.json").is_file())
        self.assertTrue((backup / "metadata" / "previous-deployment-state.json").is_file())

    def test_source_commit_counts_and_tests_are_reported(self):
        self.assertIn(f"Source commit: {self.environment.commit}", self.result.stdout)
        self.assertIn("Test result: 221 tests passed", self.result.stdout)
        self.assertRegex(self.result.stdout, r"Files copied: [1-9]\d*")
        self.assertIn("Files removed: 1", self.result.stdout)
        self.assertIn("Running process count: 0", self.result.stdout)

    def test_live_mode_can_never_be_enabled(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotRegex(script, r"auto_execute\s*=\s*\$true")
        state = json.loads((self.environment.production / "data" / "runtime.json").read_text(encoding="utf-8-sig"))
        self.assertIs(state["auto_execute"], False)

    def test_deployment_repository_branch_remains_unchanged(self):
        final = self.environment.repository_state()
        self.assertEqual(
            final["branch"], self.environment.initial_repository_state["branch"]
        )
        self.assertIn(
            f"Deployment repository branch: {final['branch']}", self.result.stdout
        )

    def test_deployment_repository_head_remains_unchanged(self):
        final = self.environment.repository_state()
        self.assertEqual(final["head"], self.environment.initial_repository_state["head"])
        self.assertIn(
            f"Deployment repository HEAD: {final['head']}", self.result.stdout
        )

    def test_deployment_repository_remains_clean(self):
        final = self.environment.repository_state()
        self.assertEqual(final["status"], "")
        self.assertIn("Deployment repository clean: True", self.result.stdout)
        self.assertFalse((self.environment.repository / ".env").exists())
        self.assertFalse((self.environment.repository / ".venv").exists())

    def test_temporary_worktree_is_removed_after_success(self):
        self.environment.assert_worktree_removed(self, self.result)

    def test_compilation_and_tests_used_temporary_worktree(self):
        worktree = self.environment.worktree_path_from_output(self.result.stdout)
        self.assertIsNotNone(worktree)
        self.assertTrue(worktree.is_relative_to(self.environment.worktree_root))
        self.assertFalse(worktree.is_relative_to(self.environment.repository))
        self.assertFalse(worktree.is_relative_to(self.environment.production))
        self.assertIn("Test result: 221 tests passed", self.result.stdout)

    def test_correct_mt5_server_is_accepted(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout)
        self.assertNotIn("MT5 server", self.result.stdout)

    def test_requested_commit_tooling_is_not_deployed(self):
        self.assertFalse(
            (self.environment.production / "scripts" / "Deploy-PrimeBot2.ps1").exists()
        )

    def test_protected_production_paths_are_not_copied_to_source_or_repository(self):
        self.assertEqual(self.result.returncode, 0, self.result.stdout)
        self.assertFalse((self.environment.repository / ".env").exists())
        self.assertFalse((self.environment.repository / ".venv").exists())
        self.assertEqual(list(self.environment.repository.glob("*.session*")), [])
        self.environment.assert_worktree_removed(self, self.result)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class DeploymentFailureIntegrationTests(unittest.TestCase):
    def _environment(self, **kwargs):
        temp_dir = RetryingTemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return DeploymentEnvironment(temp_dir.name, **kwargs)

    def test_wrong_commit_is_rejected(self):
        environment = self._environment()
        before = (environment.production / "core" / "modified.py").read_bytes()
        result = environment.deploy(expected_commit="deadbeef")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rev-parse", result.stdout)
        self.assertEqual((environment.production / "core" / "modified.py").read_bytes(), before)

    def test_dirty_repository_is_rejected(self):
        environment = self._environment()
        write(environment.repository / "dirty-local-file.txt", "dirty")
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty repository", result.stdout.lower())
        self.assertFalse((environment.production / "core" / "new_file.py").exists())

    def test_wrong_origin_is_rejected(self):
        environment = self._environment()
        environment._git("remote", "set-url", "origin", "https://example.invalid/wrong.git")
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"Wrong\s+origin URL")

    def test_identity_mismatch_is_rejected(self):
        environment = self._environment(identity_ok=False)
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Production identity mismatch", result.stdout)
        self.assertFalse((environment.production / "core" / "new_file.py").exists())

    def test_failed_tests_preserve_repository_and_remove_worktree(self):
        environment = self._environment(failing_tests=True)
        repository_before = environment.repository_state()
        runtime_before = (environment.production / "data" / "runtime.json").read_bytes()
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"Unit\s+tests failed")
        self.assertEqual(
            (environment.production / "core" / "modified.py").read_text(encoding="utf-8"),
            "VALUE = 'old production content'\n",
        )
        self.assertTrue((environment.production / "core" / "removed.py").is_file())
        self.assertFalse((environment.production / "core" / "new_file.py").exists())
        self.assertEqual(
            (environment.production / "data" / "runtime.json").read_bytes(),
            runtime_before,
        )
        self.assertEqual(environment.repository_state(), repository_before)
        environment.assert_worktree_removed(self, result)
        environment.assert_protected_unchanged(self)

    def test_failed_compilation_preserves_repository_and_removes_worktree(self):
        environment = self._environment(bad_compile=True)
        repository_before = environment.repository_state()
        runtime_before = (environment.production / "data" / "runtime.json").read_bytes()
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"Python\s+compilation failed")
        self.assertEqual(
            (environment.production / "core" / "modified.py").read_text(encoding="utf-8"),
            "VALUE = 'old production content'\n",
        )
        self.assertTrue((environment.production / "core" / "removed.py").is_file())
        self.assertFalse((environment.production / "core" / "bad_syntax.py").exists())
        self.assertFalse((environment.production / "core" / "new_file.py").exists())
        self.assertEqual(
            (environment.production / "data" / "runtime.json").read_bytes(),
            runtime_before,
        )
        self.assertEqual(environment.repository_state(), repository_before)
        environment.assert_worktree_removed(self, result)
        environment.assert_protected_unchanged(self)

    def test_older_origin_main_commit_without_deployment_scripts_is_supported(self):
        environment = self._environment()
        repository_before = environment.repository_state()
        result = environment.deploy(expected_commit=environment.older_commit[:10])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"Source commit: {environment.older_commit}", result.stdout)
        self.assertEqual(environment.repository_state(), repository_before)
        self.assertTrue(
            (environment.repository / "scripts" / "Deploy-PrimeBot2.ps1").is_file()
        )
        self.assertFalse((environment.production / "scripts").exists())
        environment.assert_worktree_removed(self, result)

    def test_local_only_commit_is_rejected(self):
        environment = self._environment()
        write(environment.repository / "core" / "local_only.py", "LOCAL_ONLY = True\n")
        environment._git("add", ".")
        environment._git("commit", "-m", "local only commit")
        local_commit = environment._git("rev-parse", "HEAD").stdout.strip()
        repository_before = environment.repository_state()
        result = environment.deploy(expected_commit=local_commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"not\s+reachable from origin/main")
        self.assertEqual(environment.repository_state(), repository_before)
        self.assertFalse((environment.production / "core" / "local_only.py").exists())

    def test_origin_commit_not_reachable_from_origin_main_is_rejected(self):
        environment = self._environment()
        environment._git("switch", "-c", "side")
        write(environment.repository / "core" / "side_only.py", "SIDE_ONLY = True\n")
        environment._git("add", ".")
        environment._git("commit", "-m", "origin side branch commit")
        side_commit = environment._git("rev-parse", "HEAD").stdout.strip()
        environment._git("push", "origin", "HEAD:side")
        environment._git("switch", "main")
        repository_before = environment.repository_state()
        result = environment.deploy(expected_commit=side_commit)
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stdout, r"not\s+reachable from origin/main")
        self.assertEqual(environment.repository_state(), repository_before)
        self.assertFalse((environment.production / "core" / "side_only.py").exists())

    def test_mt5_server_mismatch_is_rejected_without_disclosing_value(self):
        environment = self._environment(mt5_server="Wrong-Live-Server")
        result = environment.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(
            result.stdout, r"Production identity mismatch:\s+MT5 server"
        )
        self.assertNotIn("Wrong-Live-Server", result.stdout)
        self.assertNotIn("12345678", result.stdout)
        environment.assert_worktree_removed(self, result)

    def test_running_process_scope_matches_only_exact_production_main(self):
        temp_dir = RetryingTemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        inventory_path = Path(temp_dir.name) / "processes.json"
        inventory_path.write_text(
            json.dumps(
                [
                    {"ProcessId": 1, "CommandLine": 'python "C:\\PrimeBot\\main.py"'},
                    {"ProcessId": 2, "CommandLine": 'python "C:\\PrimeBot2\\main.py"'},
                    {"ProcessId": 3, "CommandLine": 'python "C:\\PrimeBot\\main.py.bak"'},
                    {"ProcessId": 4, "CommandLine": "python C:\\PrimeBot\\main.py --flag"},
                    {"ProcessId": 5, "CommandLine": "python C:\\OtherBot\\main.py"},
                ]
            ),
            encoding="utf-8",
        )
        command = (
            f"Import-Module '{DEPLOYMENT_MODULE}' -Force; "
            f"$records = Get-Content -LiteralPath '{inventory_path}' -Raw | ConvertFrom-Json; "
            "$ids = @(Get-PrimeBotTargetProcesses -ProcessRecords $records "
            "-ProductionMainPath 'C:\\PrimeBot\\main.py' | ForEach-Object { [int]$_.ProcessId }); "
            "$ids | ConvertTo-Json -Compress"
        )
        result = run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ]
        )
        ids = json.loads(result.stdout.strip())
        self.assertEqual(ids, [1, 4])


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class BootstrapIntegrationTests(unittest.TestCase):
    def test_validation_only_mode_changes_nothing(self):
        with RetryingTemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "future-repository"
            production = root / "production"
            write(production / "sentinel.txt", "untouched")
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            result = run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    BOOTSTRAP_SCRIPT,
                    "-RepositoryPath",
                    repository,
                    "-ProductionPath",
                    production,
                    "-ValidationOnly",
                ],
                check=False,
            )
            after = sorted(path.relative_to(root) for path in root.rglob("*"))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(before, after)
            self.assertFalse(repository.exists())
            self.assertEqual((production / "sentinel.txt").read_text(encoding="utf-8"), "untouched")

    def test_existing_clone_with_wrong_origin_is_rejected(self):
        with RetryingTemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "repository"
            production = root / "production"
            production.mkdir()
            run(["git", "init", repository])
            run(
                ["git", "-C", repository, "remote", "add", "origin", "https://example.invalid/wrong.git"]
            )
            result = run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    BOOTSTRAP_SCRIPT,
                    "-RepositoryPath",
                    repository,
                    "-ProductionPath",
                    production,
                    "-ValidationOnly",
                ],
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Wrong origin URL", result.stdout)

    def test_bootstrap_clones_only_repository_without_starting_or_copying_production(self):
        with RetryingTemporaryDirectory() as temp_dir:
            environment = DeploymentEnvironment(temp_dir)
            clone_path = Path(temp_dir) / "bootstrap-clone"
            sentinel = environment.production / "bootstrap-sentinel.txt"
            write(sentinel, "untouched")
            result = run(
                [
                    POWERSHELL,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    BOOTSTRAP_SCRIPT,
                    "-RepositoryPath",
                    clone_path,
                    "-ProductionPath",
                    environment.production,
                ],
                env=environment.env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((clone_path / ".git").is_dir())
            origin = run(["git", "-C", clone_path, "config", "--get", "remote.origin.url"]).stdout.strip()
            self.assertEqual(origin, APPROVED_ORIGIN)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
            self.assertNotIn("Start-ScheduledTask", result.stdout)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell is required")
class DeploymentSyntaxTests(unittest.TestCase):
    def test_powershell_syntax_validation(self):
        paths = [DEPLOY_SCRIPT, BOOTSTRAP_SCRIPT, DEPLOYMENT_MODULE]
        quoted = ",".join(f"'{path}'" for path in paths)
        command = (
            f"$failed=$false; foreach($path in @({quoted})) {{ "
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if($errors.Count -ne 0) { $failed=$true; $errors | ForEach-Object { Write-Output $_.Message } } }; "
            "if($failed) { exit 1 }"
        )
        result = run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)


class DeploymentDocumentationTests(unittest.TestCase):
    def test_one_time_vps_sequence_includes_direct_clone_and_safe_commands(self):
        content = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        self.assertIn("Install Git for Windows manually", content)
        self.assertIn(
            "git clone https://github.com/SamuelEto18/primebot-2.git C:\\PrimeBot2-Repo",
            content,
        )
        self.assertIn("-ValidationOnly", content)
        self.assertIn("-ExpectedCommit 2e7d733", content)


if __name__ == "__main__":
    unittest.main()
