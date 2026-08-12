"""提取器滚动分页重叠场景回归测试。"""

import asyncio
import sqlite3

import ima_ax_extractor
from ima_common import init_database


def test_overlapping_titles_do_not_trigger_existing_url_stop(temp_db, monkeypatch):
    """跨页重复标题不能在新文章处理前触发连续已存在终止。"""
    init_database()

    pages = [
        [
            {"element_index": 1, "title": "文章 A"},
            {"element_index": 2, "title": "文章 B"},
        ],
        [
            {"element_index": 1, "title": "文章 A"},
            {"element_index": 2, "title": "文章 B"},
            {"element_index": 3, "title": "文章 C"},
        ],
    ]
    urls = iter([
        "https://mp.weixin.qq.com/s/overlap-a",
        "https://mp.weixin.qq.com/s/overlap-b",
        "https://mp.weixin.qq.com/s/overlap-c",
    ])

    monkeypatch.setattr(ima_ax_extractor, "MAX_PAGES", 2)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_CLICK_LOAD", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_AFTER_CLOSE", 0)
    monkeypatch.setattr(ima_ax_extractor, "WAIT_SCROLL", 0)
    monkeypatch.setattr(
        ima_ax_extractor,
        "get_window_state",
        lambda _pid, _window_id: {"element_count": 100},
    )
    monkeypatch.setattr(
        ima_ax_extractor,
        "parse_articles_from_tree",
        lambda _state, _kb_name: pages.pop(0),
    )
    monkeypatch.setattr(ima_ax_extractor, "activate_ima", lambda: None)
    monkeypatch.setattr(
        ima_ax_extractor,
        "click_element",
        lambda _pid, _window_id, _element_index: True,
    )
    monkeypatch.setattr(ima_ax_extractor, "extract_url_ax", lambda *_args: next(urls))
    monkeypatch.setattr(ima_ax_extractor, "extract_title_ax", lambda: None)
    monkeypatch.setattr(ima_ax_extractor, "cmd_w_close", lambda **_kwargs: None)
    monkeypatch.setattr(
        ima_ax_extractor,
        "scroll_down",
        lambda _pid, _window_id, _amount: None,
    )
    monkeypatch.setattr(ima_ax_extractor.time, "sleep", lambda _seconds: None)

    asyncio.run(ima_ax_extractor.extract_articles(1, 1, "AI"))

    with sqlite3.connect(temp_db) as conn:
        saved_urls = {
            row[0]
            for row in conn.execute("SELECT url FROM articles")
        }

    assert "https://mp.weixin.qq.com/s/overlap-c" in saved_urls
