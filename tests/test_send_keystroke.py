"""send_keystroke: cliclick 命令构造正确性测试。

背景（systematic-debugging Phase 4）：send_keystroke 从 osascript 改为 cliclick
绕过 macOS TCC AppleEvents 限制（osascript 报错 1002「不允许发送按键」）。
cliclick 走 CGEventPost（CoreGraphics），不走 Apple Event。

这些测试**不真实执行 cliclick**（避免触发按键事件污染 CI 环境），只验证：
1. saver 内部 osascript 风格的 modifier 名（option/command/control/alt/cmd/...）
   被正确转换为 cliclick 风格（alt/cmd/ctrl/shift/fn）。
2. 命令构造顺序正确：`kd:<mods> t:<key> ku:<mods>`。
3. cliclick 未装（FileNotFoundError）时优雅降级，打印诊断而非崩溃。

不在本测试范围：
- 实际按键事件是否触发 Web Clipper（已通过交互式手动测试验证，见 commit 信息）。
- close_tab 走 send_keystroke("w", ["command"]) 的路径（属集成测试）。
"""
from unittest.mock import MagicMock, patch

import ima_obsidian_saver as saver


def _capture_run_success():
    """构造一个模拟成功完成的 subprocess.run 结果对象。"""
    r = MagicMock()
    r.returncode = 0
    r.stderr = b""
    r.stdout = b""
    return r


class TestCliclickCommandConstruction:
    """modifier 命名转换 + cmd 列表构造正确性。"""

    def test_option_shift_to_alt_shift(self):
        """osascript 'option'+'shift' → cliclick 'alt,shift'（quick_clip 路径）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["option", "shift"])
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "kd:alt,shift", "t:o", "ku:alt,shift"], (
            "option/shift 必须转换为 alt/shift，否则触发错键或 cliclick 报错")

    def test_command_shift_to_cmd_shift(self):
        """osascript 'command'+'shift' → cliclick 'cmd,shift'（clipper 路径）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["command", "shift"])
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "kd:cmd,shift", "t:o", "ku:cmd,shift"], (
            "command 必须映射为 cmd（cliclick 不接受 command）")

    def test_command_only_to_cmd(self):
        """单 'command' → cliclick 'cmd'（close_tab 走 Cmd+W 路径）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("w", ["command"])
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "kd:cmd", "t:w", "ku:cmd"]

    def test_cmd_alias(self):
        """'cmd'（osascript 别名）直接透传 cliclick 'cmd'。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("w", ["cmd"])
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "kd:cmd", "t:w", "ku:cmd"]

    def test_no_modifiers(self):
        """无 modifier：cmd 只含 t:<key>，无 kd/ku（trigger_clipper_and_save 中 'return'）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("return", [])
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "t:return"]

    def test_none_modifiers(self):
        """modifiers=None：与 [] 等价（默认值兼容老调用）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("return")
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "t:return"]

    def test_unknown_modifier_passthrough(self):
        """未在 _CLICLICK_MOD_MAP 中的 modifier 名原样透传（让 cliclick 自己报错）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("x", ["weird"])
        args, _ = mock_run.call_args
        assert args[0] == ["cliclick", "kd:weird", "t:x", "ku:weird"]

    def test_timeout_passed_through(self):
        """timeout=5 必须透传 subprocess.run（防 keystroke 永久挂起导致 saver 卡死）。"""
        with patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["option", "shift"])
        _, kwargs = mock_run.call_args
        assert kwargs.get("timeout") == 5
        assert kwargs.get("capture_output") is True


class TestCliclickNotInstalled:
    """FileNotFoundError 优雅降级（cliclick 未 brew install）。"""

    def test_file_not_found_does_not_raise(self, capsys):
        """cliclick 不在 PATH 时 send_keystroke 不得抛异常，且打印诊断。"""
        with patch.object(saver.subprocess, "run", side_effect=FileNotFoundError("no cliclick")):
            saver.send_keystroke("o", ["option", "shift"])
        captured = capsys.readouterr()
        assert "cliclick 未安装" in captured.out, "缺失时应打印安装提示"
        assert "brew install cliclick" in captured.out

    def test_file_not_found_no_modifiers(self, capsys):
        """无 modifier 路径也走 FileNotFoundError 降级。"""
        with patch.object(saver.subprocess, "run", side_effect=FileNotFoundError()):
            saver.send_keystroke("return", [])
        captured = capsys.readouterr()
        assert "cliclick 未安装" in captured.out


class TestCliclickFailureDiagnosis:
    """cliclick returncode != 0 时打印 stderr 诊断（保留原 osascript 诊断语义）。"""

    def test_nonzero_returncode_prints_stderr(self, capsys):
        """rc != 0 时打印 rc + stderr 片段，方便 launchd 排障。"""
        fail = MagicMock()
        fail.returncode = 70  # cliclick 真实错误码区间
        fail.stderr = b"cliclick: Error: invalid modifier name 'foobar'"
        fail.stdout = b""
        with patch.object(saver.subprocess, "run", return_value=fail):
            saver.send_keystroke("o", ["option", "shift"])
        captured = capsys.readouterr()
        assert "send_keystroke" in captured.out
        assert "rc=70" in captured.out
        assert "invalid modifier name" in captured.out

    def test_nonzero_returncode_truncates_long_stderr(self, capsys):
        """stderr > 200 字节时截断（防 launchd 日志爆掉）。"""
        fail = MagicMock()
        fail.returncode = 1
        fail.stderr = b"X" * 500
        fail.stdout = b""
        with patch.object(saver.subprocess, "run", return_value=fail):
            saver.send_keystroke("o", ["option"])
        captured = capsys.readouterr()
        # 截断到 200 字符
        assert "X" * 200 in captured.out
        assert "X" * 201 not in captured.out
