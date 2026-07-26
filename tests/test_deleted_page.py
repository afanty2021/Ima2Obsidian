"""微信「文章已被发布者删除」页检测 + 短路处理。

背景：部分微信文章在 saver 访问时已被发布者删除（页面显示「该内容已被发布者删除」/
「此内容因违规已删除」），永远无法保存。若保持未保存状态，每次运行都会反复打开该页
（0 落盘 → failed_count++ → 触发上游告警），且 stats 待保存永久卡着这篇。

策略：检测到删除页 → mark_deleted 把 status 改为 'deleted' → 自动被所有
WHERE status='success' 查询排除，永久跳过，不计 failed。与验证页（临时可恢复）
不同，删除页是永久状态，命中后短路返回，不再触发 quick_clip。
"""
from unittest.mock import patch

import pytest

import ima_obsidian_saver as saver


class TestDeletedReason:
    """_deleted_reason: 永久不可恢复页判定 + reason 映射（单源，只查 body）。"""

    def test_hit_publisher_deleted(self):
        """body 含「该内容已被发布者删除」→ 命中 reason='发布者删除'"""
        assert saver._deleted_reason({"text": "该内容已被发布者删除"}) == "发布者删除"

    def test_hit_violation_deleted_old(self):
        """body 含「此内容因违规已删除」（旧文案）→ '违规不可查看'"""
        assert saver._deleted_reason({"text": "此内容因违规已删除"}) == "违规不可查看"

    def test_hit_violation_unavailable_new(self):
        """body 含「此内容因违规无法查看」（新文案）→ '违规不可查看'"""
        assert saver._deleted_reason({"text": "此内容因违规无法查看"}) == "违规不可查看"

    def test_hit_blocked_account(self):
        """body 含「此账号已被屏蔽」（前缀匹配）→ '账号被屏蔽'"""
        assert saver._deleted_reason({"text": "此账号已被屏蔽，内容无法查看"}) == "账号被屏蔽"

    def test_miss_normal_article(self):
        """正常文章 body 不命中"""
        assert saver._deleted_reason({"title": "别只循环听英文歌", "text": "正文内容"}) is None

    def test_miss_verify_page(self):
        """微信验证页不应被判为删除页（验证页可恢复、删除页永久，处理路径不同）"""
        assert saver._deleted_reason({"title": "验证", "text": "当前环境异常，完成验证"}) is None

    def test_none_snapshot(self):
        assert saver._deleted_reason(None) is None

    def test_empty_snapshot(self):
        assert saver._deleted_reason({}) is None

    def test_long_article_with_phrase_not_deleted(self):
        """合法长文章 body 引用删除整句（讨论审查/媒体类）→ 不误判

        阈值是防误判的关键——合法文章 body 远超 60 字。
        """
        long_body = ("近日有读者发现某公众号文章打开后提示该内容已被发布者删除，"
                     "据悉该文章此前因违规被投诉。" + "详细情况分析" * 20)
        assert len(long_body) > 60  # 前置：确实是长 body
        assert saver._deleted_reason({"title": "媒体报道", "text": long_body}) is None

    def test_legit_title_with_keyword_body_empty_not_deleted(self):
        """合法文章 title 含关键词短语但 body 为空（慢加载）→ 不命中（只查 body，防标题误杀）

        新增第 4 关键词「此账号已被屏蔽」是名词性短语，合法文章 title 可能含此短语
        （如「评此账号已被屏蔽现象」）。若 _deleted_reason 并 title 扫描，慢加载
        body='' 时 title+body <60 + 子串命中 → mark_deleted 永久跳过合法文章。
        只查 body 防此误杀。
        """
        snap = {"title": "评此账号已被屏蔽现象", "text": ""}
        assert saver._deleted_reason(snap) is None


class TestSaveOneArticleDeletedPath:
    """save_one_article 检测到删除页须短路返回 ('deleted', None)，不触发 quick_clip。"""

    @pytest.fixture
    def isolated_vault(self, tmp_path, monkeypatch):
        vault = tmp_path / "Vault"
        vault.mkdir()
        clip_dir = vault / "Clippings"
        clip_dir.mkdir()
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
        return vault, clip_dir

    def test_deleted_page_short_circuits(self, isolated_vault):
        """删除页：返回 ('deleted', None)，不调 trigger_quick_clip/find_and_rename，关闭标签"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "已删文章", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信", "text": "该内容已被发布者删除"}), \
             patch("ima_obsidian_saver.activate_browser") as mock_activate, \
             patch("ima_obsidian_saver.trigger_quick_clip") as mock_clip, \
             patch("ima_obsidian_saver.find_and_rename_in_vault") as mock_rename, \
             patch("ima_obsidian_saver.close_tab") as mock_close, \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None), f"删除页应短路返回 ('deleted', None)，实际: {result!r}"
        mock_clip.assert_not_called()        # 不应触发 Web Clipper（删除页无文章内容）
        mock_rename.assert_not_called()      # 不应查找/重命名
        mock_activate.assert_not_called()
        mock_close.assert_called_once()      # 仍应关闭标签

    def test_normal_page_not_treated_as_deleted(self, isolated_vault):
        """正常文章页：不触发删除短路，走正常保存流程返回 ('saved', date)"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "正常文章", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "正常文章", "text": "这是正文内容"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault", return_value=(True, "260101")), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("saved", "260101"), f"正常文章应返回 ('saved', date)，实际: {result!r}"

    def test_snapshot_none_does_not_short_circuit(self, isolated_vault):
        """读快照失败（JS 异常返回 None）→ 不当删除处理，降级走正常流程（避免误杀真实文章）"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "文章", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot", return_value=None), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault", return_value=(True, "260101")), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        # 快照读不到 → 不应误判删除，应走正常流程（此处 mock 命中 → saved）
        assert result[0] == "saved", f"快照失败时不应误判删除，实际: {result!r}"
