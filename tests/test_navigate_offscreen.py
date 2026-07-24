"""navigate_to_kb 对屏外窗口的两种情况分别处理（code-review #1）。

- y<-50（窗口被移到屏外，同 Space）：bring_to_front 只 activate 不移动位置，须 restart_ima 重置
- is_on_screen=False（窗口在别的 Space/隐藏）：bring_to_front 切 Space 拉前台，不调 restart_ima
"""
from unittest.mock import patch

import ima_incremental_update


def _win(is_on_screen, y):
    return {"pid": 1, "window_id": 1, "is_on_screen": is_on_screen,
            "bounds": {"x": 0, "y": y, "width": 1512, "height": 885}}


def test_y_offscreen_calls_restart_ima():
    """y<-50 屏外窗口 → 调 restart_ima 重置位置（bring_to_front 不移动位置）"""
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(True, -100), _win(True, 33)]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI")
    mock_restart.assert_called_once()


def test_is_on_screen_false_uses_bring_to_front_not_restart():
    """is_on_screen=False（别的 Space）→ bring_to_front，不调 restart_ima"""
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(False, 33), _win(True, 33)]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value='{"tree_markdown":""}') as mock_cua, \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI")
    mock_restart.assert_not_called()
    # bring_to_front 经 run_cua 调用
    assert any("bring_to_front" in " ".join(str(a) for a in c.args) for c in mock_cua.call_args_list)


def test_combo_is_on_screen_false_and_y_offscreen_also_restarts():
    """is_on_screen=False 且 y<-50 的组合 → bring_to_front 切回 Space 后仍须复查 y 并 restart_ima

    旧 if/elif 互斥逻辑下，is_on_screen=False 命中 if 后 elif(y<-50) 永不评估，组合情况的
    y<-50 得不到复位。改顺序 if 后，bring_to_front 后复查 y 仍<-50 → 触发 restart_ima。
    """
    with patch("ima_incremental_update.get_ima_main_window",
               side_effect=[_win(False, -100), _win(True, -100), _win(True, 33)]), \
         patch("ima_incremental_update.restart_ima") as mock_restart, \
         patch("ima_incremental_update.run_cua", return_value='{"tree_markdown":""}'), \
         patch("ima_incremental_update.subprocess.run"), \
         patch("ima_incremental_update.time.sleep"):
        ima_incremental_update.navigate_to_kb("AI")
    mock_restart.assert_called_once()
