"""navigate_to_kb 所有 attempts 失败后强制 restart_ima 兜底（launchd GUI session 隔离自愈）。

根因（systematic-debugging 2026-07-29 11:50 跑）：
- launchd 进程不在用户 GUI session 中，cua-driver bring_to_front（NSRunningApplication.activate）
  不生效，窗口未拉到前台 → AX 探测读到 0 元素 → 5 个知识库导航全失败。
- 旧代码所有 attempts 失败后直接 return False，无自愈。

修复：所有 attempts 失败后强制 restart_ima + 递归一次重试。
- 防无限递归：max_attempts > 1 守卫，递归调用传 max_attempts=1
- restart_ima 异常 fail-soft 仍 return False（保留原契约）
"""
import json
from unittest.mock import patch

import ima_incremental_update


def _win(is_on_screen=True, y=33):
    """构造一个在屏幕内的窗口（避免触发前置 is_on_screen/y<-50 兜底）"""
    return {"pid": 1, "window_id": 1, "is_on_screen": is_on_screen,
            "bounds": {"x": 0, "y": y, "width": 1512, "height": 885}}


def _empty_md_json():
    """cua-driver 0 元素响应（AX Tree 不完整）"""
    return '{"tree_markdown":""}'


def _good_md_json():
    """cua-driver 完整 AX Tree 响应：包含目标 KB 'AI' 入口（element 2）

    AXStaticText 数 >= 5（通过完整性校验），且第 3 行完全匹配 'AI'。
    """
    md = (
        '[0] AXWindow "AI - ima.copilot"\n'
        '[1] AXScrollArea\n'
        '[2] AXStaticText = "AI"\n'
        '[3] AXStaticText = "侧边栏其他内容 1"\n'
        '[4] AXStaticText = "侧边栏其他内容 2"\n'
        '[5] AXStaticText = "侧边栏其他内容 3"\n'
        '[6] AXStaticText = "侧边栏其他内容 4"\n'
        '[7] AXStaticText = "侧边栏其他内容 5"\n'
    )
    return json.dumps({"tree_markdown": md})


def test_all_attempts_fail_triggers_restart_ima_fallback():
    """5 次 attempts 全失败（AX Tree 0 元素）→ 触发 restart_ima 兜底自愈

    复现 launchd GUI session 隔离场景：cua-driver 一直返回 0 元素 tree_markdown，
    现有逻辑只能走完 max_attempts 然后 return False。修复后应强制 restart_ima 一次。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(), _win()]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    # 兜底触发：restart_ima 被调用至少 1 次
    assert mock_restart.call_count >= 1
    # 递归调用 max_attempts=1，最终仍失败（AX Tree 0 元素）→ return False
    assert result is False


def test_fallback_uses_recursion_with_max_attempts_1_to_prevent_infinite_loop():
    """兜底递归调用 navigate_to_kb(max_attempts=1) 防止无限循环

    关键守卫：max_attempts > 1 才触发兜底。递归调用传 max_attempts=1 后，
    即使再次全失败也不会再触发 restart_ima（避免 launchd 持续后台时无限重启）。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(), _win()]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    # 严格断言：只调 1 次（外层兜底），递归层 max_attempts=1 不再触发
    mock_restart.assert_called_once()


def test_fallback_recursion_succeeds_returns_true():
    """兜底重试时 AX Tree 恢复正常 + 找到 KB → 返回 True（自愈成功）

    场景：restart_ima 后窗口重新渲染，AX Tree 完整、侧边栏能找到 KB 入口。
    验证兜底路径能正常 return True，不被异常路径污染。
    """
    # 外层 5 次 attempts × 2 次读（首次 + retry）= 10 次 0 元素响应；
    # 递归层 1 次 attempt：首次读 good_md（≥5 元素通过 + 命中 KB）→ 1 次 click
    cua_responses = [_empty_md_json()] * 10 + [_good_md_json(), '{"clicked": true}']

    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(), _win()]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", side_effect=cua_responses), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"), \
         patch("ima_incremental_update.get_ax_window_title",
               return_value="AI - ima.copilot"), \
         patch("ima_incremental_update.is_on_kb_list", return_value=True):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    assert result is True
    mock_restart.assert_called_once()


def test_fallback_restart_ima_exception_returns_false():
    """restart_ima 抛异常时被捕获，fail-soft 仍 return False（保留原契约）"""
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima",
               side_effect=RuntimeError("quit timeout")), \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=5)
    assert result is False


def test_max_attempts_1_does_not_trigger_fallback():
    """max_attempts=1（递归调用场景）失败后不再触发 restart_ima（守卫正确）

    防无限递归的关键：max_attempts=1 时即使全失败也直接 return False，
    不进入兜底分支。这保证递归层不会再递归。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               return_value=_win()), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value=_empty_md_json()), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        result = ima_incremental_update.navigate_to_kb("AI", max_attempts=1)
    assert result is False
    # 守卫生效：max_attempts=1 时不能再调 restart_ima
    mock_restart.assert_not_called()
