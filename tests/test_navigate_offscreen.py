"""navigate_to_kb 对屏外窗口的两种情况分别处理（code-review #1）。

- y<-50（窗口被移到屏外，同 Space）：bring_to_front 只 activate 不移动位置，须 restart_ima 重置
- is_on_screen=False（窗口在别的 Space/隐藏）：bring_to_front 切 Space 拉前台，不调 restart_ima

注：所有用例传 allow_restart=False 隔离「末尾 launchd 兜底 restart」对前置窗口修复断言的干扰
（末尾兜底由 test_restart_ima_fallback.py 覆盖）。allow_restart 是 code review #5 新增参数，
解耦 max_attempts（循环次数）与是否允许末尾 restart 兜底。

mock 策略：用 _window_sequence callable 而非 side_effect 列表——代码增加
get_ima_main_window 调用次数时（如 feee25a 的 wait_for_ax_ready fresh_window）不会
StopIteration（preexisting 测试失败根因）。
"""
from unittest.mock import patch

import pytest

import ima_incremental_update


@pytest.fixture(autouse=True)
def _reset_nav_restart_budget():
    """y<-50 前置复位现计入 nav restart 预算（10/2 评审 Important #2）——模块级
    计数器跨用例泄漏会让本文件用例依赖执行顺序（前面用例耗尽预算后，
    y<-50 用例不再 restart），一律前后清零。"""
    ima_incremental_update._NAV_RESTARTS_THIS_RUN = 0
    yield
    ima_incremental_update._NAV_RESTARTS_THIS_RUN = 0


def _win(is_on_screen, y):
    return {"pid": 1, "window_id": 1, "is_on_screen": is_on_screen,
            "bounds": {"x": 0, "y": y, "width": 1512, "height": 885}}


def _window_sequence(*first_windows, normal=None):
    """side_effect callable：先按序返回 first_windows，之后一直返回 normal。

    比 side_effect 列表稳健——navigate_to_kb 流程增加 get_ima_main_window 调用次数时
    （如 wait_for_ax_ready 的 fresh_window 复查）不会 StopIteration。
    normal 默认 _win(True, 33)（正常窗口，is_on_screen=True, y=33）。
    """
    if normal is None:
        normal = _win(True, 33)
    state = {"i": 0}

    def side_effect():
        if state["i"] < len(first_windows):
            w = first_windows[state["i"]]
        else:
            w = normal
        state["i"] += 1
        return w
    return side_effect


def test_y_offscreen_calls_restart_ima():
    """y<-50 屏外窗口 → 调 restart_ima 重置位置（bring_to_front 不移动位置）"""
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=_window_sequence(_win(True, -100))), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_common.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5, allow_restart=False)
    mock_restart.assert_called_once()


def test_is_on_screen_false_uses_bring_to_front_not_restart():
    """is_on_screen=False（别的 Space）→ bring_to_front，不调 restart_ima"""
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=_window_sequence(_win(False, 33))), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_common.run_cua", return_value='{"tree_markdown":""}') as mock_cua, \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5, allow_restart=False)
    mock_restart.assert_not_called()
    # bring_to_front 经 run_cua 调用
    assert any("bring_to_front" in " ".join(str(a) for a in c.args) for c in mock_cua.call_args_list)


def test_y_offscreen_restart_counts_into_run_budget():
    """y<-50 屏外复位必须计入 nav restart 预算（10/2 评审 Important #2：
    原实现漏计——单条导航链最多 1 次未计（此处）+ 1 次已计（末尾兜底），
    且 AX 死亡与屏外几何常并发，封顶名存实亡）"""
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=_window_sequence(_win(True, -100))), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_common.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5, allow_restart=False)
    mock_restart.assert_called_once()
    assert ima_incremental_update._NAV_RESTARTS_THIS_RUN == 1


def test_y_offscreen_skips_restart_when_budget_exhausted():
    """预算耗尽后 y<-50 不再 restart（有界保证），继续用原窗口走导航"""
    ima_incremental_update._NAV_RESTARTS_THIS_RUN = (
        ima_incremental_update.MAX_NAV_RESTARTS_PER_RUN)
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=_window_sequence(_win(True, -100))), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_common.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5, allow_restart=False)
    mock_restart.assert_not_called()  # 耗尽旁路生效，不烧第 4 次 restart


def test_combo_is_on_screen_false_and_y_offscreen_also_restarts():
    """is_on_screen=False 且 y<-50 的组合 → bring_to_front 切回 Space 后仍须复查 y 并 restart_ima

    旧 if/elif 互斥逻辑下，is_on_screen=False 命中 if 后 elif(y<-50) 永不评估，组合情况的
    y<-50 得不到复位。改顺序 if 后，bring_to_front 后复查 y 仍<-50 → 触发 restart_ima。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=_window_sequence(_win(False, -100), _win(True, -100))), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_common.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI", max_attempts=5, allow_restart=False)
    mock_restart.assert_called_once()
