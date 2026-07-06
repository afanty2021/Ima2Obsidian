"""#3: find_and_rename_in_vault 必须把内容覆盖的 date_str 传回 save_one_article

避免 extract_publish_date 降级为"今天"、文件按内容真实日期命名、DB 却存今天的日期。
"""
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from ima_obsidian_saver import find_and_rename_in_vault


@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """隔离的 vault + Clippings 目录"""
    vault = tmp_path / "Vault"
    vault.mkdir()
    clip_dir = vault / "Clippings"
    clip_dir.mkdir()
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    return vault, clip_dir


def _write_clip_with_date(clip_dir, title, content_date_str):
    """在 Clippings 写一个 .md 文件，正文含 *YYYY年M月D日* 标记"""
    # 模拟 Web Clipper 刚保存的文件：文件名是文章标题，正文含发布日期
    f = clip_dir / f"{title}.md"
    y, m, d = content_date_str
    content = f"# {title}\n\n正文内容\n\n*{y}年{m}月{d}日 10:00*\n"
    f.write_text(content, encoding="utf-8")
    # 调整 mtime 为最近，确保 find_and_rename_in_vault 能扫到
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime))
    return f


def test_returns_content_date_when_extract_publish_date_fell_back_to_today(isolated_vault):
    """extract_publish_date 降级为今天时，find_and_rename_in_vault 用内容日期重命名后，
    必须把"内容日期"传回调用方，让 saver 把真实日期存进 DB published_date。
    """
    vault, clip_dir = isolated_vault
    title = "一篇真实发布于 2025 年 3 月 4 日的测试文章标题"
    _write_clip_with_date(clip_dir, title, (2025, 3, 4))

    # 模拟 extract_publish_date 失败降级为今天的场景
    today_str = datetime.now().strftime("%y%m%d")

    existing_files = set()  # 空 set，让 find_and_rename 视所有文件为新文件
    result = find_and_rename_in_vault(title, today_str, existing_files, target_folder=None)

    # 新返回签名：(renamed, actual_date_used)
    assert isinstance(result, tuple), "find_and_rename_in_vault 应返回 (bool, Optional[str])"
    renamed, actual_date = result
    assert renamed is True
    assert actual_date == "250304", (
        f"应返回内容日期 250304 而非今天的 {today_str}；"
        f"文件已被命名为 250304 但 DB 会存错日期"
    )


def test_returns_none_date_when_not_renamed(isolated_vault):
    """未找到匹配文件时，返回 (False, None)"""
    vault, clip_dir = isolated_vault
    result = find_and_rename_in_vault(
        "完全不存在的标题XXXXXXXX", "260101", set(), target_folder=None,
    )
    renamed, actual_date = result
    assert renamed is False
    assert actual_date is None


def test_returns_input_date_when_no_content_date(isolated_vault):
    """文件存在但内容无日期标记时，返回输入的 date_str"""
    vault, clip_dir = isolated_vault
    title = "另一篇没有日期标记的测试文章标题YYYYYYY"
    (clip_dir / f"{title}.md").write_text(f"# {title}\n纯正文无日期\n", encoding="utf-8")

    result = find_and_rename_in_vault(title, "260101", set(), target_folder=None)
    renamed, actual_date = result
    assert renamed is True
    assert actual_date == "260101"  # 沿用输入日期


def test_save_one_article_propagates_content_date_to_main(tmp_path, monkeypatch):
    """save_one_article 必须把 find_and_rename_in_vault 返回的内容日期传给 mark_saved

    通过 mock 完整流程：mock extract_publish_date 降级为今天，
    mock find_and_rename_in_vault 返回内容日期，验证 save_one_article 返回该日期。
    """
    vault = tmp_path / "Vault"
    vault.mkdir()
    clip_dir = vault / "Clippings"
    clip_dir.mkdir()
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)

    # Mock 整个浏览器交互链，使 save_one_article 能跑通到末尾
    today_str = datetime.now().strftime("%y%m%d")
    real_date = "250304"  # 内容里的真实日期

    article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "测试", "kb": "AI"}
    browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

    with patch("ima_obsidian_saver.extract_publish_date", return_value=today_str), \
         patch("ima_obsidian_saver.open_url"), \
         patch("ima_obsidian_saver.activate_browser"), \
         patch("ima_obsidian_saver.trigger_quick_clip"), \
         patch("ima_obsidian_saver.close_tab"), \
         patch("ima_obsidian_saver.find_and_rename_in_vault",
               return_value=(True, real_date)):
        from ima_obsidian_saver import save_one_article
        success, date_str = save_one_article(article, browser_config)

    assert success is True
    assert date_str == real_date, (
        f"save_one_article 应传回内容日期 {real_date} 而非 extract_publish_date 的 {today_str}；"
        f"否则 main 会把错误日期写进 DB"
    )


class TestSaveOneArticleTupleContract:
    """save_one_article 元组契约：所有路径必须返回 (bool, Optional[str]) 二元组"""

    def test_failure_returns_false_none(self, tmp_path, monkeypatch):
        """renamed=False 时返回 (False, None)，不能只返回 False 或 (False, today_str)"""
        vault = tmp_path / "Vault"
        vault.mkdir()
        clip_dir = vault / "Clippings"
        clip_dir.mkdir()
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)

        article = {"id": 1, "url": "https://example.com/x", "title": "测试", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault",
                   return_value=(False, None)):  # 模拟文件未找到
            from ima_obsidian_saver import save_one_article
            result = save_one_article(article, browser_config)

        # 必须是二元组，不是 bool，不是单值
        assert isinstance(result, tuple) and len(result) == 2, \
            f"应返回二元组，实际: {result!r}"
        success, date_str = result
        assert success is False
        assert date_str is None, "失败时不应返回日期（main 不应据此调 mark_saved）"

    def test_dry_run_returns_true_and_date(self, tmp_path, monkeypatch):
        """dry_run 返回 (True, date_str)，让 main 能据此打印但不持久化"""
        vault = tmp_path / "Vault"
        vault.mkdir()
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", vault / "Clippings")

        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "测试", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="250304"):
            from ima_obsidian_saver import save_one_article
            success, date_str = save_one_article(article, browser_config, dry_run=True)

        assert success is True
        assert date_str == "250304"
