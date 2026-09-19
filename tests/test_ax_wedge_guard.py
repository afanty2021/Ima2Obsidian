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

import pytest

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
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)


def test_wedged_at_page_start_aborts_without_clicks(temp_db, monkeypatch):
    """页循环开头即检测到脱落 → 激活重读一次仍脱落 → 中止本库，不产生任何点击"""
    from ima_common import init_database

    init_database()
    activations = []
    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 5)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 0)  # 自愈预算清零 → 走中止分支
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
        [{"element_index": 1, "title": "新文章甲的标题足够长以通过幻影校验"}],
        [{"element_index": 2, "title": "后续新文章"}],
    ]
    # 卡1 幻影：读到别的文章；重试探测到脱落 → 放弃；页2 再遇脱落 → 中止
    urls = iter(["https://mp.weixin.qq.com/s/stale"])
    page_titles = iter(["旧文章"])
    clicked = []
    states = [dict(HEALTHY_STATE), dict(WEDGED_STATE), dict(WEDGED_STATE), dict(WEDGED_STATE)]

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 3)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 0)  # 自愈预算清零 → 走中止分支
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
    monkeypatch.setattr(ima_ax_extractor, "_SCROLL_LOCAL_COORDS", {})
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


class TestVerifyKbOrExit:
    """运行前 KB 预检（_verify_kb_or_exit）。

    背景（评审 2026-09-17）：旧实现基于 get_kb_window_title——该函数只会返回
    「包含 kb_name 的标题」或空串，窗口停在别的知识库时必然拿到空串走进放行分支，
    sys.exit(2) 硬中止对其注释声称的跨库场景是死代码。现改用与走库漂移守卫
    同源的 _kb_visible_in_any_window 三态判定。"""

    def test_on_target_kb_passes(self, monkeypatch, capsys):
        monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window",
                            lambda _kb: True)
        ima_ax_extractor._verify_kb_or_exit("英语教与学")
        assert "确认在 英语教与学" in capsys.readouterr().out

    def test_drifted_aborts_with_exit_2(self, monkeypatch, capsys):
        # 窗口停在别的知识库（如重启后恢复到上次浏览的 KB）→ 必须硬中止
        monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window",
                            lambda _kb: False)
        with pytest.raises(SystemExit) as exc:
            ima_ax_extractor._verify_kb_or_exit("英语教与学")
        assert exc.value.code == 2
        out = capsys.readouterr().out
        assert "防跨库污染" in out and "英语教与学" in out

    def test_unreadable_titles_passes_with_warning(self, monkeypatch, capsys):
        # 标题全空/读失败（Electron 冷启动）→ 放行，走库守卫兜底
        monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window",
                            lambda _kb: None)
        ima_ax_extractor._verify_kb_or_exit("英语教与学")
        assert "继续尝试提取" in capsys.readouterr().out


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
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
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


def test_wedge_heal_at_page_start_recovers_and_continues(temp_db, monkeypatch):
    """页首脱落 → 激活重读仍脱落 → 自愈（重启+重新导航）→ 刷新句柄重解析并继续走库"""
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 5)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 5)
    # 页1 顶/重读均脱落；自愈后页2-5 健康（标题全在库）：前两页消耗自愈宽限，
    # 后两页连续全已知触发预检停止
    states = [dict(WEDGED_STATE), dict(WEDGED_STATE)] + [
        dict(HEALTHY_STATE) for _ in range(4)
    ]
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: states.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: [{"element_index": 1, "title": "库内文章甲"}],
    )
    monkeypatch.setattr(
        ima_ax_extractor, "get_ima_main_window",
        lambda: {"pid": 9, "window_id": 9, "bounds": {"width": 1512, "height": 949}},
    )
    calls = {"restart": 0, "navigate": 0}

    def fake_restart():
        calls["restart"] += 1
        return True

    def fake_navigate(kb, allow_restart=True):
        calls["navigate"] += 1
        return True

    monkeypatch.setattr(ima_incremental_update, "restart_ima", fake_restart)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", fake_navigate)
    _patch_common(monkeypatch)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(
        ima_ax_extractor, "click_element", lambda *_a: (_ for _ in ()).throw(AssertionError("不应点击")))

    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status) VALUES "
            "('https://mp.weixin.qq.com/s/known', '库内文章甲', '英语教与学', 'success')"
        )

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "英语教与学"))

    assert calls == {"restart": 1, "navigate": 1}
    assert calls["restart"] <= ima_ax_extractor.MAX_WEDGE_RESTARTS_PER_KB


def test_wedge_heal_mid_card_resumes_and_retries_card(temp_db, monkeypatch):
    """卡间脱落 → 自愈 → 弃卡重解析当前页（不滚动），已完成卡去重跳过、弃卡补试成功"""
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    pages = [
        [
            {"element_index": 1, "title": "新文章甲的标题足够长以通过幻影校验"},
            {"element_index": 2, "title": "新文章乙的标题同样足够长以便校验"},
        ],
        [
            {"element_index": 1, "title": "新文章甲的标题足够长以通过幻影校验"},
            {"element_index": 2, "title": "新文章乙的标题同样足够长以便校验"},
        ],
    ]
    # 甲 正常入库；乙 第一次读 URL 失败 → 探测脱落 → 自愈 → 重解析后乙补试成功
    urls = iter([
        "https://mp.weixin.qq.com/s/jia",  # 甲
        None,                               # 乙 未读到 URL → 探测脱落 → 自愈
        "https://mp.weixin.qq.com/s/yi",    # 乙 重解析后补试成功
    ])
    page_titles = iter(["新文章甲的标题足够长以通过幻影校验", "新文章乙的标题同样足够长以便校验"])
    clicked = []
    scrolls = []
    states = [dict(HEALTHY_STATE), dict(WEDGED_STATE), dict(HEALTHY_STATE)]

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 2)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 5)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
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
        lambda: {"pid": 9, "window_id": 9, "bounds": {"width": 1512, "height": 949}},
    )

    def fake_restart():
        return True

    def fake_navigate(kb, allow_restart=True):
        return True

    monkeypatch.setattr(ima_incremental_update, "restart_ima", fake_restart)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", fake_navigate)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)

    def fake_scroll(*_a):
        scrolls.append(True)

    monkeypatch.setattr(ima_ax_extractor, "scroll_down", fake_scroll)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 甲 入库；乙 在自愈后的重解析页上补试成功
    assert "https://mp.weixin.qq.com/s/yi" in saved_urls
    # 甲 点击一次、乙 失败前点击一次、重解析后补试一次
    assert clicked == [1, 2, 2]
    # 自愈页不得滚动（滚动会跳过未处理的弃卡）——仅页 2 走完后的正常翻页滚动一批
    assert len(scrolls) == 10


def test_wedge_mid_list_heal_descends_past_processed_pages(temp_db, monkeypatch):
    """中段脱落几何回归（复审 61b4eb5 Important#1）：页1 全部处理完 → 页2 首卡脱落
    → 自愈后重解析从列表顶开始，重走已处理页三计数全零，若无宽限「本页无进展」
    会立刻截停续走、弃卡丢失。宽限机制下必须穿过已处理页下降回弃卡并补试入库。"""
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    page1 = [
        {"element_index": 1, "title": "新文章甲的标题足够长以通过幻影校验"},
        {"element_index": 2, "title": "新文章乙的标题同样足够长以便校验"},
    ]
    page2 = [
        {"element_index": 3, "title": "新文章丙的标题一样足够长以便校验通过"},
        {"element_index": 4, "title": "新文章丁的标题仍旧足够长以便校验"},
    ]
    # 迭代序：页1(甲乙入库) → 页2(丙脱落自愈) → 重解析回顶部(甲乙去重跳过，宽限)
    # → 滚动回页2(丙补试+丁入库，解除宽限) → 页2 再解析(全已处理，无进展停止)
    parse_sequence = [page1, page2, page1, page2, page2]
    # 甲、乙、丙(失败一次)、丙补试、丁
    urls = iter([
        "https://mp.weixin.qq.com/s/jia", "https://mp.weixin.qq.com/s/yi",
        None, "https://mp.weixin.qq.com/s/bing", "https://mp.weixin.qq.com/s/ding",
    ])
    titles = iter([
        "新文章甲的标题足够长以通过幻影校验", "新文章乙的标题同样足够长以便校验",
        "新文章丙的标题一样足够长以便校验通过", "新文章丁的标题仍旧足够长以便校验",
    ])
    clicked, scrolls = [], []
    states = [dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(HEALTHY_STATE)]

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 10)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 5)
    monkeypatch.setattr(ima_ax_extractor, "get_window_state",
                        lambda _p, _w: states.pop(0))
    monkeypatch.setattr(ima_ax_extractor, "parse_articles_from_tree",
                        lambda _state, _kb: parse_sequence.pop(0))
    monkeypatch.setattr(ima_ax_extractor, "get_ima_main_window",
                        lambda: {"pid": 9, "window_id": 9,
                                 "bounds": {"width": 1512, "height": 949}})
    calls = {"restart": 0, "navigate": 0}

    def fake_restart():
        calls["restart"] += 1
        return True

    def fake_navigate(kb, allow_restart=True):
        calls["navigate"] += 1
        return True

    monkeypatch.setattr(ima_incremental_update, "restart_ima", fake_restart)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", fake_navigate)
    _patch_common(monkeypatch)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    def click(_pid, _wid, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(titles))
    monkeypatch.setattr(ima_ax_extractor, "scroll_down",
                        lambda *_a, **_kw: scrolls.append(True))
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 四篇文章全部入库——弃卡丙经「重解析顶部 → 宽限下降 → 补试」链路回收
    assert saved_urls == {
        "https://mp.weixin.qq.com/s/jia", "https://mp.weixin.qq.com/s/yi",
        "https://mp.weixin.qq.com/s/bing", "https://mp.weixin.qq.com/s/ding",
    }
    # 甲乙 → 丙(失败) → 重解析页去重跳过不点击 → 丙补试 → 丁
    assert clicked == [1, 2, 3, 3, 4]
    assert calls == {"restart": 1, "navigate": 1}
    # 自愈页(丙失败那次)不滚动；页1、宽限下降页、补试页各滚动一批
    assert len(scrolls) == 30


def test_two_heals_in_one_walk_accumulate_budget_and_resume(temp_db, monkeypatch):
    """单库连续两次自愈：预算叠加消耗（restarts==2）、两次弃卡都在重解析页补试入库。

    注意本测试的 parse 序列在自愈后下一页即达弃卡（零下降页），不锁定宽限语义
    ——宽限二次赋值（宽限期内二次自愈、残余宽限不足）由
    test_second_heal_during_active_grace_reassigns_descent_budget 真锁定。"""
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status) VALUES "
            "('https://mp.weixin.qq.com/s/known-seed', '库内已知文章', 'AI', 'success')"
        )

    t_jia = "新文章甲的标题足够长以通过幻影校验"
    t_yi = "新文章乙的标题同样足够长以便校验"
    t_bing = "新文章丙的标题长度也足以通过幻影校验"
    t_known = "库内已知文章"
    pages = [
        [{"element_index": 1, "title": t_jia}],
        [{"element_index": 1, "title": t_yi}],
        [{"element_index": 1, "title": t_yi}],
        [{"element_index": 1, "title": t_bing}],
        [{"element_index": 1, "title": t_bing}],
        [{"element_index": 1, "title": t_known}],
    ]
    urls = iter([
        "https://mp.weixin.qq.com/s/jia",   # 甲 正常入库
        None,                                # 乙 未读到 URL → 探测脱落 → 自愈 1
        "https://mp.weixin.qq.com/s/yi",    # 乙 重解析补试成功
        None,                                # 丙 未读到 URL → 探测脱落 → 自愈 2
        "https://mp.weixin.qq.com/s/bing",  # 丙 重解析补试成功
    ])
    page_titles = iter([t_jia, t_yi, t_bing])
    # 消费序 8 次：P1/P2/探测/P3(重解析)/P4/探测/P5(重解析)/P6 —— 卡间路径无重读
    states = [dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE)]
    restarts = {"n": 0}
    parsed = []

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 6)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 5)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: states.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: (parsed.append(1), pages.pop(0))[1],
    )
    monkeypatch.setattr(
        ima_ax_extractor, "get_ima_main_window",
        lambda: {"pid": 9, "window_id": 9, "bounds": {"width": 1512, "height": 949}},
    )
    monkeypatch.setattr(ima_incremental_update, "restart_ima", lambda: restarts.update(n=restarts["n"] + 1) or True)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", lambda kb, allow_restart=True: True)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(ima_ax_extractor, "click_element", lambda _p, _w, _e: True)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved = {row[0] for row in conn.execute("SELECT url FROM articles")}

    assert restarts["n"] == 2  # 两次自愈，预算 5 未耗尽
    assert {"https://mp.weixin.qq.com/s/jia",
            "https://mp.weixin.qq.com/s/yi",
            "https://mp.weixin.qq.com/s/bing"} <= saved
    assert len(parsed) == 6  # 走满页预算而非中途截停


def test_heal_budget_exact_exhaustion_boundary(temp_db, monkeypatch):
    """预算边界：MAX_WEDGE_RESTARTS_PER_KB=2 时，第 2 次自愈（额度内最后一次）
    必须被允许并成功续走，第 3 次脱落被拒（restarts 不再增长）——钉住 off-by-one。

    序列即「预算未耗尽的成功自愈后再脱落」：乙 靠自愈1 入库（成功自愈），
    丙 靠自愈2（恰好用尽预算）入库，丁 脱落时被拒计失败留待下轮。"""
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    t_jia = "新文章甲的标题足够长以通过幻影校验"
    t_yi = "新文章乙的标题同样足够长以便校验"
    t_bing = "新文章丙的标题长度也足以通过幻影校验"
    t_ding = "新文章丁的标题长度亦足以通过幻影校验"
    pages = [
        [{"element_index": 1, "title": t_jia}],
        [{"element_index": 1, "title": t_yi}],
        [{"element_index": 1, "title": t_yi}],
        [{"element_index": 1, "title": t_bing}],
        [{"element_index": 1, "title": t_bing}],
        [{"element_index": 1, "title": t_ding}],
    ]
    urls = iter([
        "https://mp.weixin.qq.com/s/jia",   # 甲 正常入库
        None,                                # 乙 → 探测脱落 → 自愈 1（成功自愈）
        "https://mp.weixin.qq.com/s/yi",    # 乙 补试成功
        None,                                # 丙 → 探测脱落 → 自愈 2（恰好用尽预算）
        "https://mp.weixin.qq.com/s/bing",  # 丙 补试成功
        None,                                # 丁 → 自愈被拒 → 计失败留待下轮
    ])
    page_titles = iter([t_jia, t_yi, t_bing])
    # 状态消费序 9 次：P1/P2/探测/P3(重解析)/P4/探测/P5(重解析)/P6/探测
    # = H H W H H W H H W —— 卡间自愈路径探测后不重读（区别于页首路径的激活重读）
    states = [dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(WEDGED_STATE)]
    restarts = {"n": 0}
    parsed = []

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 6)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 2)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: states.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: (parsed.append(1), pages.pop(0))[1],
    )
    monkeypatch.setattr(
        ima_ax_extractor, "get_ima_main_window",
        lambda: {"pid": 9, "window_id": 9, "bounds": {"width": 1512, "height": 949}},
    )
    monkeypatch.setattr(ima_incremental_update, "restart_ima", lambda: restarts.update(n=restarts["n"] + 1) or True)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", lambda kb, allow_restart=True: True)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(ima_ax_extractor, "click_element", lambda _p, _w, _e: True)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 恰好用尽预算的两次自愈都成功续走并入库
    assert restarts["n"] == 2
    assert {"https://mp.weixin.qq.com/s/jia",
            "https://mp.weixin.qq.com/s/yi",
            "https://mp.weixin.qq.com/s/bing"} <= saved
    # 丁 在预算耗尽后被拒：不入库（留待下轮），但本轮正常走完不崩溃
    assert "https://mp.weixin.qq.com/s/ding" not in saved
    assert len(parsed) == 6  # MAX_PAGES=6：丁 被拒后仍正常走完页循环


def test_second_heal_during_active_grace_reassigns_descent_budget(temp_db, monkeypatch):
    """宽限二次赋值真锁定（复审 55e86ff vacuous 补课）：宽限期内二次自愈时，
    heal_resume_grace 必须重赋值为新 page+1 而非保留残余——本序列在第二次自愈时
    残余宽限仅 1 页，而到弃卡还需 2 个纯下降页：若赋值丢失（只在 grace==0 时
    赋值），第二个下降页会触发「本页无进展」截停，弃卡 戊 丢失。

    序列：P1[甲乙]入库 → P2[丙]入库 → P3[戊]卡间脱落→自愈1(grace=4) →
    P4-P6 三个已处理下降页(grace 4→1) → P7 页首脱落→自愈2(grace 重赋 8) →
    P8-P9 两个下降页 → P10[戊]补试入库(解除宽限) → P11 已处理页无进展正常停。
    """
    import ima_incremental_update
    from ima_common import init_database

    init_database()
    t_jia = "新文章甲的标题足够长以通过幻影校验"
    t_yi = "新文章乙的标题同样足够长以便校验"
    t_bing = "新文章丙的标题长度也足以通过幻影校验"
    t_wu = "新文章戊的标题长度亦足以通过幻影校验"
    page12 = [{"element_index": 1, "title": t_jia}, {"element_index": 2, "title": t_yi}]
    page3 = [{"element_index": 3, "title": t_bing}]
    page_wu = [{"element_index": 4, "title": t_wu}]
    pages = [page12, page3, page_wu,          # P1-P3：P3 卡间脱落
             page12, page3, page12,            # P4-P6：下降（grace 4→1）
             page12, page3,                    # P8-P9：二次自愈后下降（重赋 8）
             page_wu, page_wu]                 # P10 补试入库 / P11 无进展停
    urls = iter([
        "https://mp.weixin.qq.com/s/jia", "https://mp.weixin.qq.com/s/yi",
        "https://mp.weixin.qq.com/s/bing",
        None,                                  # P3 戊 未读到 URL → 探测脱落 → 自愈 1
        "https://mp.weixin.qq.com/s/wu",       # P10 戊 补试入库
    ])
    page_titles = iter([t_jia, t_yi, t_bing, t_wu])
    # 消费序 13 次：P1/P2/P3+探测 / P4/P5/P6 / P7 页首(读+重读均脱落) / P8/P9 / P10/P11
    states = [dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(HEALTHY_STATE),
              dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(HEALTHY_STATE),
              dict(WEDGED_STATE), dict(WEDGED_STATE),
              dict(HEALTHY_STATE), dict(HEALTHY_STATE), dict(HEALTHY_STATE),
              dict(HEALTHY_STATE)]
    restarts = {"n": 0}
    parsed = []

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 15)
    monkeypatch.setattr(ima_ax_extractor, "MAX_WEDGE_RESTARTS_PER_KB", 5)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(ima_ax_extractor, "close_all_article_tabs", lambda: 0)
    monkeypatch.setattr(ima_ax_extractor, "_kb_visible_in_any_window", lambda _kb: True)
    monkeypatch.setattr(
        ima_ax_extractor, "get_window_state",
        lambda _p, _w: states.pop(0),
    )
    monkeypatch.setattr(
        ima_ax_extractor, "parse_articles_from_tree",
        lambda _state, _kb: (parsed.append(1), pages.pop(0))[1],
    )
    monkeypatch.setattr(
        ima_ax_extractor, "get_ima_main_window",
        lambda: {"pid": 9, "window_id": 9, "bounds": {"width": 1512, "height": 949}},
    )
    monkeypatch.setattr(ima_incremental_update, "restart_ima", lambda: restarts.update(n=restarts["n"] + 1) or True)
    monkeypatch.setattr(ima_incremental_update, "navigate_to_kb", lambda kb, allow_restart=True: True)
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)

    clicked = []

    def click(_p, _w, element_index):
        clicked.append(element_index)
        return True

    monkeypatch.setattr(ima_ax_extractor, "click_element", click)
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_a: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: next(page_titles))
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kw: None)
    monkeypatch.setattr(ima_ax_extractor, "scroll_down", lambda *_a: None)
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _s: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved = {row[0] for row in conn.execute("SELECT url FROM articles")}

    # 四篇全入库：弃卡 戊 穿过两段宽限下降最终补试成功
    assert saved == {"https://mp.weixin.qq.com/s/jia",
                     "https://mp.weixin.qq.com/s/yi",
                     "https://mp.weixin.qq.com/s/bing",
                     "https://mp.weixin.qq.com/s/wu"}
    assert restarts["n"] == 2
    # 甲乙丙各一次 + 戊 失败一次 + 戊 补试一次；下降页的已处理卡零点击
    assert clicked == [1, 2, 3, 4, 4]
    # 10 次解析走完全序列：宽限期内没有任何停止条件提前截停
    assert len(parsed) == 10
