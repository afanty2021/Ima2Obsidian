"""send_keystroke: cliclick 命令构造正确性测试。

背景（systematic-debugging Phase 4）：send_keystroke 从 osascript 改为 cliclick
绕过 macOS TCC AppleEvents 限制（osascript 报错 1002「不允许发送按键」）。
cliclick 走 CGEventPost（CoreGraphics），不走 Apple Event。

路径解析（launchd 兼容）：cliclick 路径在 import 时由 _find_cliclick() 检测并
缓存到 _CLICLICK_PATH 模块级常量。launchd 启动的 Python 进程不继承用户 shell
的 PATH（默认仅 /usr/bin:/bin:/usr/sbin:/sbin，不含 Homebrew），故不能依赖
PATH 查找——必须用绝对路径调用。未找到时提前 return 打印诊断（不抛异常）。

这些测试**不真实执行 cliclick**（避免触发按键事件污染 CI 环境），只验证：
1. saver 内部 osascript 风格的 modifier 名（option/command/control/alt/cmd/...）
   被正确转换为 cliclick 风格（alt/cmd/ctrl/shift/fn）。
2. 命令构造顺序正确：`kd:<mods> t:<key> ku:<mods>`，且首元素为 _CLICLICK_PATH
   绝对路径（不是字符串 "cliclick"，防 PATH 假设回归）。
3. cliclick 未找到（_CLICLICK_PATH=None）时优雅降级，打印诊断且不调 subprocess.run。

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
    """modifier 命名转换 + cmd 列表构造正确性。

    所有测试显式 patch _CLICLICK_PATH='/fake/cliclick'，让断言独立于运行环境
    （不依赖本机是否装了 cliclick），同时验证首元素是绝对路径（而非 "cliclick"
    字符串，防 cab0a2c 类 PATH 假设回归）。
    """

    def test_option_shift_to_alt_shift(self):
        """osascript 'option'+'shift' → cliclick 'alt,shift'（quick_clip 路径）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["option", "shift"])
        mock_run.assert_called_once()
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "kd:alt,shift", "t:o", "ku:alt,shift"], (
            "option/shift 必须转换为 alt/shift，否则触发错键或 cliclick 报错")

    def test_command_shift_to_cmd_shift(self):
        """osascript 'command'+'shift' → cliclick 'cmd,shift'（clipper 路径）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["command", "shift"])
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "kd:cmd,shift", "t:o", "ku:cmd,shift"], (
            "command 必须映射为 cmd（cliclick 不接受 command）")

    def test_command_only_to_cmd(self):
        """单 'command' → cliclick 'cmd'（close_tab 走 Cmd+W 路径）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("w", ["command"])
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "kd:cmd", "t:w", "ku:cmd"]

    def test_cmd_alias(self):
        """'cmd'（osascript 别名）直接透传 cliclick 'cmd'。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("w", ["cmd"])
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "kd:cmd", "t:w", "ku:cmd"]

    def test_no_modifiers(self):
        """无 modifier：cmd 只含 t:<key>，无 kd/ku（trigger_clipper_and_save 中 'return'）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("return", [])
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "t:return"]

    def test_none_modifiers(self):
        """modifiers=None：与 [] 等价（默认值兼容老调用）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("return")
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "t:return"]

    def test_unknown_modifier_passthrough(self):
        """未在 _CLICLICK_MOD_MAP 中的 modifier 名原样透传（让 cliclick 自己报错）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("x", ["weird"])
        args, _ = mock_run.call_args
        assert args[0] == ["/fake/cliclick", "kd:weird", "t:x", "ku:weird"]

    def test_timeout_passed_through(self):
        """timeout=5 必须透传 subprocess.run（防 keystroke 永久挂起导致 saver 卡死）。"""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["option", "shift"])
        _, kwargs = mock_run.call_args
        assert kwargs.get("timeout") == 5
        assert kwargs.get("capture_output") is True

    def test_cmd_first_element_is_absolute_path_not_string(self):
        """防 cab0a2c 回归：cmd[0] 必须是绝对路径（来自 _CLICLICK_PATH），不是 'cliclick'。

        launchd 启动的进程不继承用户 shell PATH（默认 /usr/bin:/bin:/usr/sbin:/sbin，
        不含 Homebrew），用字符串 'cliclick' 会在 launchd 下触发 FileNotFoundError。
        """
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=_capture_run_success()) as mock_run:
            saver.send_keystroke("o", ["option"])
        args, _ = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "/fake/cliclick", (
            "cmd[0] 必须是 _CLICLICK_PATH 绝对路径，不能是字符串 'cliclick'（launchd PATH 限制）")
        assert cmd[0] != "cliclick", "字符串 'cliclick' 会在 launchd 下 FileNotFoundError"


class TestCliclickNotFound:
    """_CLICLICK_PATH=None（cliclick 未装）时优雅降级。

    新设计下：cliclick 路径在 import 时由 _find_cliclick() 检测一次，未找到则
    _CLICLICK_PATH=None，send_keystroke 提前 return 打印诊断（不调 subprocess.run，
    不会触发 FileNotFoundError）。
    """

    def test_none_path_does_not_raise(self, capsys):
        """_CLICLICK_PATH=None 时 send_keystroke 不得抛异常，且打印诊断。"""
        with patch.object(saver, "_CLICLICK_PATH", None), \
             patch.object(saver.subprocess, "run") as mock_run:
            saver.send_keystroke("o", ["option", "shift"])
        captured = capsys.readouterr()
        assert "cliclick 未安装" in captured.out, "缺失时应打印安装提示"
        assert "brew install cliclick" in captured.out
        mock_run.assert_not_called(), "_CLICLICK_PATH=None 时不得调用 subprocess.run"

    def test_none_path_no_modifiers(self, capsys):
        """无 modifier 路径也走 None 降级。"""
        with patch.object(saver, "_CLICLICK_PATH", None), \
             patch.object(saver.subprocess, "run") as mock_run:
            saver.send_keystroke("return", [])
        captured = capsys.readouterr()
        assert "cliclick 未安装" in captured.out
        mock_run.assert_not_called()


class TestCliclickFailureDiagnosis:
    """cliclick returncode != 0 时打印 stderr 诊断（保留原 osascript 诊断语义）。"""

    def test_nonzero_returncode_prints_stderr(self, capsys):
        """rc != 0 时打印 rc + stderr 片段，方便 launchd 排障。"""
        fail = MagicMock()
        fail.returncode = 70  # cliclick 真实错误码区间
        fail.stderr = b"cliclick: Error: invalid modifier name 'foobar'"
        fail.stdout = b""
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=fail):
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
        with patch.object(saver, "_CLICLICK_PATH", "/fake/cliclick"), \
             patch.object(saver.subprocess, "run", return_value=fail):
            saver.send_keystroke("o", ["option"])
        captured = capsys.readouterr()
        # 截断到 200 字符
        assert "X" * 200 in captured.out
        assert "X" * 201 not in captured.out

