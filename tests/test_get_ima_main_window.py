"""get_ima_main_window 必须排除文章标签页独立窗口（含浏览器地址栏），选 KB 列表主窗口。

背景：IMA 改版后文章在独立标签页窗口打开（含"地址和搜索栏"），与 KB 列表主窗口
面积相近甚至更大。旧逻辑只按面积 max 选窗口，会把面积更大的文章窗口误当主窗口，
导致 navigate_to_kb 在文章窗口里找不到知识库元素 → 0 产出。
"""
import json
from unittest.mock import patch

import ima_common


def _win(wid, title, height=885):
    """构造一个 ima.copilot 窗口（width=1512，与实测一致）"""
    return {
        "window_id": wid,
        "pid": 65349,
        "app_name": "ima.copilot",
        "title": title,
        "is_on_screen": True,
        "bounds": {"x": 0, "y": 0, "width": 1512, "height": height},
    }


MAIN_WIN = _win(7936, "AI", height=885)  # KB 列表主窗口
# 文章窗口故意 height 更大 → 面积更大 → 旧逻辑 max 会误选它（复现真实 bug）
ARTICLE_WIN = _win(7975, "突发！让张益唐苦熬7年...Fable 5...", height=950)


def _run_cua_with(windows, article_wids):
    """mock run_cua：list_windows 返回 windows；get_window_state 对 article_wids 返回含地址栏的 md。"""
    def fake(args, timeout=30):
        if args and args[0] == "list_windows":
            return json.dumps({"windows": windows})
        if args and args[0] == "call" and len(args) > 1 and args[1] == "get_window_state":
            wid = json.loads(args[2]).get("window_id")
            if wid in article_wids:
                return json.dumps({
                    "tree_markdown": "- [0] AXWindow\n- [1] AXTextField 地址和搜索栏\n",
                    "element_count": 523,
                })
            return json.dumps({
                "tree_markdown": "- [0] AXWindow 'AI'\n- [1] AXButton (知识库)\n",
                "element_count": 545,
            })
        return ""
    return fake


class TestGetImaMainWindow:

    def test_picks_kb_window_over_larger_article_window(self):
        """文章窗口面积更大时，仍必须选 KB 主窗口（按地址栏排除文章窗口）"""
        with patch("ima_common.run_cua", side_effect=_run_cua_with([MAIN_WIN, ARTICLE_WIN], {7975})):
            mw = ima_common.get_ima_main_window()
        assert mw is not None
        assert mw["window_id"] == 7936  # 主窗口，非面积更大的文章窗口 7975

    def test_single_kb_window_selected(self):
        """只有 KB 主窗口时正常选中"""
        with patch("ima_common.run_cua", side_effect=_run_cua_with([MAIN_WIN], set())):
            mw = ima_common.get_ima_main_window()
        assert mw["window_id"] == 7936

    def test_falls_back_when_only_article_windows(self):
        """只有文章窗口（无主窗口）时不返回 None，回退选面积最大者（降级由下游处理）"""
        with patch("ima_common.run_cua", side_effect=_run_cua_with([ARTICLE_WIN], {7975})):
            mw = ima_common.get_ima_main_window()
        assert mw is not None  # 不卡死，返回某窗口

    def test_get_window_state_failure_does_not_crash(self):
        """get_window_state 抛异常时不应崩溃（无法排除则回退面积逻辑）"""
        def fake(args, timeout=30):
            if args and args[0] == "list_windows":
                return json.dumps({"windows": [MAIN_WIN, ARTICLE_WIN]})
            if args and args[0] == "call":
                raise RuntimeError("cua-driver failed: exit 1")
            return ""
        with patch("ima_common.run_cua", side_effect=fake):
            mw = ima_common.get_ima_main_window()
        assert mw is not None  # 回退，不崩
