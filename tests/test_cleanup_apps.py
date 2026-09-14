"""运行收尾（cleanup_gui_apps / --no-cleanup）的单元与入口测试

2026-09-14 用户要求：GUI 自动化任务完成后收尾，退出 IMA/Obsidian/Chrome。
挂在 main 的 finally 上——失败退出（sys.exit(1)）/中断也照常收尾。
同日评审修复：
- Critical：TTY 交互运行跳过收尾（不强退用户正在用的应用）；
  launchd/后台（非 TTY）无条件收尾——显式接受的权衡；
- Important：pgrep 探活后再 quit（对未运行应用发 quit 会把它启动）；
  检查 osascript 返回码/stderr（TCC 拒权不能无感）。
注意：模块顶层绑定的 real_cleanup 是真实函数对象；conftest 的 autouse
只替换模块属性，不影响该引用。
"""
import json
import subprocess
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import ima_incremental_update as inc
# 收集期绑定真实函数（conftest autouse 之后会把模块属性换成 no-op mock）
from ima_incremental_update import cleanup_gui_apps as real_cleanup


@pytest.fixture
def quit_mock(monkeypatch):
    mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", mock)
    return mock


class TestCleanupGuiAppsUnit:
    def test_quits_running_apps_via_osascript(self, quit_mock):
        def fake_run(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if cmd[0] == "pgrep":
                # Obsidian 未运行，其余在跑
                result.returncode = 0 if cmd[2] != "Obsidian" else 1
            return result

        quit_mock.side_effect = fake_run
        real_cleanup()
        osascript_cmds = [" ".join(c.args[0]) for c in quit_mock.call_args_list
                          if c.args[0][0] == "osascript"]
        # Important：对未运行的应用不发 quit（否则会把它启动）
        assert len(osascript_cmds) == 2
        assert any("ima.copilot" in c for c in osascript_cmds)
        assert any('"Google Chrome"' in c for c in osascript_cmds)
        assert not any('"Obsidian"' in c for c in osascript_cmds)

    def test_interactive_skips_all(self, quit_mock):
        real_cleanup(interactive=True)
        quit_mock.assert_not_called()

    def test_osascript_failure_logged_not_raised(self, quit_mock):
        # Important：rc!=0（如 TCC 拒权）不能无感，也不能中断收尾
        def fake_run(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return MagicMock(returncode=0)
            return MagicMock(returncode=1, stderr=b"execution error: Not authorized")

        quit_mock.side_effect = fake_run
        real_cleanup()  # 不抛异常，三个应用都尝试
        assert sum(1 for c in quit_mock.call_args_list
                   if c.args[0][0] == "osascript") == 3

    def test_osascript_timeout_does_not_block_rest(self, quit_mock):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "pgrep":
                return MagicMock(returncode=0)
            if "ima.copilot" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)
            return MagicMock(returncode=0)

        quit_mock.side_effect = fake_run
        real_cleanup()  # 不抛异常
        assert sum(1 for c in quit_mock.call_args_list
                   if c.args[0][0] == "osascript") == 3

    def test_daemon_killed_when_running(self, quit_mock):
        real_cleanup()
        pkill_cmds = [c.args[0] for c in quit_mock.call_args_list
                      if c.args[0][0] == "pkill"]
        assert pkill_cmds == [["pkill", "-f", "cua-driver serve"]]

    def test_daemon_skipped_when_not_running(self, quit_mock):
        def fake_run(cmd, **kwargs):
            result = MagicMock(returncode=0)
            if cmd[0] == "pgrep" and cmd[2] == "cua-driver serve":
                result.returncode = 1
            return result

        quit_mock.side_effect = fake_run
        real_cleanup()
        assert not any(c.args[0][0] == "pkill" for c in quit_mock.call_args_list)


class TestMainCleanupWiring:
    """入口级：真实运行收尾 / --no-cleanup 跳过 / dry-run 跳过 / 失败退出也收尾 /
    gate 跳过路径不收尾 / TTY 交互跳过"""

    @pytest.fixture
    def quiet_run(self, monkeypatch, tmp_path, temp_db):
        """mock 掉全部 IMA 副作用的完整可跑 main()"""
        monkeypatch.setattr(inc, "LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr(inc, "ensure_daemon", lambda: True)
        monkeypatch.setattr(inc, "save_snapshot_and_report_drift",
                            lambda: ({}, {"chrome_profile_name": "James",
                                          "clipper_version": "1.7.1",
                                          "ime_source": "com.apple.keylayout.ABC"}))
        monkeypatch.setattr(inc.subprocess, "Popen", MagicMock())
        monkeypatch.setattr(inc.time, "sleep", lambda s: None)
        monkeypatch.setattr(inc, "update_knowledge_base",
                            lambda kb, dry_run: {"new": 0, "skipped": 0, "failed": 0})

    def test_real_run_triggers_cleanup(self, quiet_run, monkeypatch, _quiet_app_cleanup):
        monkeypatch.setattr(sys, "argv", ["ima_incremental_update.py", "--kb", "AI"])
        inc.main()
        _quiet_app_cleanup.assert_called_once_with(interactive=False)

    def test_no_cleanup_flag_skips(self, quiet_run, monkeypatch, _quiet_app_cleanup):
        monkeypatch.setattr(
            sys, "argv", ["ima_incremental_update.py", "--kb", "AI", "--no-cleanup"])
        inc.main()
        _quiet_app_cleanup.assert_not_called()

    def test_dry_run_skips_cleanup(self, monkeypatch, tmp_path, _quiet_app_cleanup):
        monkeypatch.setattr(inc, "LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--dry-run", "--kb", "AI"])
        monkeypatch.setattr(inc, "update_knowledge_base",
                            lambda kb, dry_run: {"new": 0, "skipped": 0, "failed": 0})
        inc.main()
        _quiet_app_cleanup.assert_not_called()

    def test_cleanup_runs_on_failing_exit(self, quiet_run, monkeypatch,
                                          _quiet_app_cleanup):
        # 有失败 → sys.exit(1)，finally 里的收尾仍须执行
        monkeypatch.setattr(inc, "update_knowledge_base",
                            lambda kb, dry_run: {"new": 0, "skipped": 0, "failed": 1})
        monkeypatch.setattr(sys, "argv", ["ima_incremental_update.py", "--kb", "AI"])

        with pytest.raises(SystemExit) as exc_info:
            inc.main()

        assert exc_info.value.code == 1
        _quiet_app_cleanup.assert_called_once()

    def test_scheduled_gate_skip_does_not_cleanup(self, monkeypatch, tmp_path,
                                                  _quiet_app_cleanup):
        # gate 跳过路径在 try 之前 return：什么都没启动，也不该触发收尾
        class FakeDatetime(datetime):
            @classmethod
            def now(cls):
                return datetime(2026, 9, 1, 17, 10)

        monkeypatch.setattr(inc, "datetime", FakeDatetime)
        monkeypatch.setattr(inc, "LOCK_FILE", tmp_path / "l.lock")
        state = tmp_path / "last_incremental_run.json"
        monkeypatch.setattr(inc, "RUN_STATE_FILE", state)
        state.write_text(json.dumps({"date": "2026-09-01", "kb_failed": 0,
                                     "save_failed": 0, "full": True,
                                     "ts": "2026-09-01T16:31:04"}),
                         encoding="utf-8")
        temp_db = tmp_path / "gate.db"
        monkeypatch.setattr("ima_common.DB_FILE", temp_db)
        from ima_common import init_database
        init_database()
        monkeypatch.setattr(sys, "argv",
                            ["ima_incremental_update.py", "--scheduled"])

        inc.main()  # gate 跳过，正常返回
        _quiet_app_cleanup.assert_not_called()

    def test_interactive_tty_run_skips_cleanup(self, quiet_run, monkeypatch,
                                               _quiet_app_cleanup):
        # Critical 修复：TTY 手动运行不强退用户正在用的应用
        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        monkeypatch.setattr(sys, "stdout", fake_stdout)
        monkeypatch.setattr(sys, "argv", ["ima_incremental_update.py", "--kb", "AI"])

        inc.main()

        _quiet_app_cleanup.assert_called_once_with(interactive=True)
