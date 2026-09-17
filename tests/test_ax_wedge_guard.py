"""AX 脱落（仅菜单栏）防护回归测试。

背景：2026-09-17 英语教与学 实测——新版 ima 的 web AX 被 macOS 回收成仅菜单栏
（element_count 仍 >100，数量阈值区分不了），提取器在此态下点击按在死元素上、
extract_url_ax 对窗口的每次走查耗满 run_cua 30s 超时，一张卡磨 8-10 分钟，
表现为整轮「卡死」。防护：页循环与幻影重试前检测脱落形态，激活重读一次仍脱落
则中止本库提取（已提取文章照常进入保存阶段）；根治手段为 ima NSAppSleepDisabled。
"""
import asyncio
import json
import sqlite3
from unittest.mock import patch

import ima_ax_extractor
from ima_ax_extractor import ax_tree_menu_bar_only

# 仅菜单栏形态：有 AXMenuBar/AXMenuItem、无 AXStaticText，element_count 仍 >100
WEDGED_MD = (
    "- [0] AXMenuBar\n"
    "  - [1] AXMenuBarItem \"Apple\"\n"
    "    - [2] AXMenuItem \"关于本机\"\n"
)
WEDGED_STATE = {"element_count": 262, "tree_markdown": WEDGED_MD}
HEALTHY_MD = (
    "- [0] AXWindow \"英语教与学\"\n"
    "- [12] AXStaticText = \"个人知识库\"\n"
    "- [14] AXStaticText = \"英语教与学\"\n"
)
HEALTHY_STATE = {"element_count": 500, "tree_markdown": HEALTHY_MD}


class TestAxTreeMenuBarOnly:
    def test_menu_only_is_wedged(self):
        assert ax_tree_menu_bar_only(WEDGED_MD)

    def test_healthy_tree_not_wedged(self):
        assert not ax_tree_menu_bar_only(HEALTHY_MD)

    def test_empty_md_not_wedged(self):
        # 空 md 视为未知（读失败），不得误判脱落触发中止
        assert not ax_tree_menu_bar_only("")
        assert not ax_tree_menu_bar_only(None or "")

    def test_menubar_with_statictext_not_wedged(self):
        md = "- [0] AXMenuBar\n- [1] AXStaticText = \"菜单内文本\"\n"
        assert not ax_tree_menu_bar_only(md)


def _patch_common(monkeypatch):
    """提取流程公共 mock：无真实 cua-driver / 系统调用"""
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)


def test_wedged_at_page_start_aborts_without_clicks(temp_db, monkeypatch):
    """页循环开头即检测到脱落 → 激活重读一次仍脱落 → 中止本库，不产生任何点击"""
    from ima_common import init_database

    init_database()
    activations = []
    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 5)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: dict(WEDGED_STATE),
    )
    _patch_common(monkeypatch)

    def activate():
        activations.append(True)

    monkeypatch.setattr(ima_ax_extractor, "activate_ima", activate)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "英语教与学"))

    assert len(activations) == 1  # 脱落路径的一次激活重读
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0


def test_wedge_detected_in_verify_loop_skips_reclick(temp_db, monkeypatch):
    """幻影重试前探测到脱落 → 放弃重试（不浪费点击与 30s 级读取），卡片计失败"""
    from ima_common import init_database

    init_database()
    pages = [
        [{"element_index": 1, "title": "新文章甲"}],
        [{"element_index": 2, "title": "后续新文章"}],
    ]
    # 卡1 幻影：读到别的文章；重试探测到脱落 → 放弃；页2 再遇脱落 → 中止
    urls = iter(["https://mp.weixin.qq.com/s/stale"])
    page_titles = iter(["旧文章"])
    clicked = []
    states = [dict(HEALTHY_STATE), dict(WEDGED_STATE), dict(WEDGED_STATE), dict(WEDGED_STATE)]

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 3)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: states.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: pages.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "get_ima_main_window",
        lambda: {"pid": 1, "window_id": 1, "bounds": {"width": 1512, "height": 949}},
    )

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    _patch_common(monkeypatch)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 只有初始点击：幻影重试被脱落探测拦下
    assert clicked == [1]
    assert "https://mp.weixin.qq.com/s/stale" not in saved_urls


def test_scroll_down_uses_targeted_wheel_path(monkeypatch):
    """新版 ima 列表只吃定向滚轮（window-local x/y），不吃合成按键——
    scroll_down 必须携带 window_id + 局部坐标（cua-driver px 路径）。"""
    captured = {}
    monkeypatch.setattr(ima_ax_extractor, "run_cua_call",
                        lambda tool, params: captured.update(tool=tool, params=params) or {})
    monkeypatch.setattr(ima_ax_extractor, "_SCROLL_LOCAL_COORDS", None)
    monkeypatch.setattr(
        ima_ax_extractor, "run_cua",
        lambda *_a: json.dumps({"windows": [
            {"window_id": 77, "bounds": {"width": 1512, "height": 949}},
            {"window_id": 88, "bounds": {"width": 500, "height": 500}},
        ]}),
    )

    ima_ax_extractor.scroll_down(123, 77, amount=3)

    assert captured["tool"] == "scroll"
    p = captured["params"]
    assert p["pid"] == 123 and p["window_id"] == 77
    assert p["direction"] == "down" and p["amount"] == 3
    assert p["x"] == 756 and p["y"] == 427  # 1512*0.5, 949*0.45


class TestKbVisibleInAnyWindow:
    """KB 漂移守卫的窗口标题判定（list_windows CG 标题，launchd 可读）"""

    def _run_cua_with(self, windows):
        def fake(args, timeout=30):
            if args and args[0] == "list_windows":
                return json.dumps({"windows": windows})
            return ""
        return fake

    def test_target_kb_visible(self, monkeypatch):
        monkeypatch.setattr(ima_ax_extractor, "run_cua", self._run_cua_with([
            {"app_name": "ima", "bounds": {"height": 949}, "title": "英语教与学 - 已固定 - ima.copilot"},
        ]))
        assert ima_ax_extractor._kb_visible_in_any_window("英语教与学") is True

    def test_drifted_to_other_kb(self, monkeypatch):
        monkeypatch.setattr(ima_ax_extractor, "run_cua", self._run_cua_with([
            {"app_name": "ima", "bounds": {"height": 949}, "title": "皮皮鲁的知识库"},
        ]))
        assert ima_ax_extractor._kb_visible_in_any_window("英语教与学") is False

    def test_all_titles_empty_unknown(self, monkeypatch):
        # 标题全空（Electron 冷启动）→ 无法判定 → None（放行，不误中止）
        monkeypatch.setattr(ima_ax_extractor, "run_cua", self._run_cua_with([
            {"app_name": "ima", "bounds": {"height": 949}, "title": ""},
        ]))
        assert ima_ax_extractor._kb_visible_in_any_window("英语教与学") is None

    def test_read_failure_unknown(self, monkeypatch):
        def boom(*_a):
            raise RuntimeError("daemon down")
        monkeypatch.setattr(ima_ax_extractor, "run_cua", boom)
        assert ima_ax_extractor._kb_visible_in_any_window("英语教与学") is None


def test_kb_drift_aborts_walk_without_clicks(temp_db, monkeypatch):
    """页循环检测到窗口离开目标 KB → 立即中止（防跨库污染），不点击任何卡片"""
    from ima_common import init_database

    init_database()
    clicked = []
    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 5)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: dict(HEALTHY_STATE),
    )
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: False)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "英语教与学"))

    assert clicked == []
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 0
