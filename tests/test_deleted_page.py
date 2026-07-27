"""微信「文章已被发布者删除」页检测 + 短路处理。

背景：部分微信文章在 saver 访问时已被发布者删除（页面显示「该内容已被发布者删除」/
「此内容因违规已删除」），永远无法保存。若保持未保存状态，每次运行都会反复打开该页
（0 落盘 → failed_count++ → 触发上游告警），且 stats 待保存永久卡着这篇。

策略：检测到删除页 → mark_deleted 把 status 改为 'deleted' → 自动被所有
WHERE status='success' 查询排除，永久跳过，不计 failed。与验证页（临时可恢复）
不同，删除页是永久状态，命中后短路返回，不再触发 quick_clip。
"""
import os
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

        阈值是防误判的关键——合法文章 body 远超 100 字。
        """
        long_body = ("近日有读者发现某公众号文章打开后提示该内容已被发布者删除，"
                     "据悉该文章此前因违规被投诉。" + "详细情况分析" * 20)
        assert len(long_body) > 100  # 前置：确实是长 body
        assert saver._deleted_reason({"title": "媒体报道", "text": long_body}) is None

    def test_legit_title_with_keyword_body_empty_not_deleted(self):
        """合法文章 title 含关键词短语但 body 为空（慢加载）→ 不命中（只查 body，防标题误杀）

        新增第 4 关键词「此账号已被屏蔽」是名词性短语，合法文章 title 可能含此短语
        （如「评此账号已被屏蔽现象」）。若 _deleted_reason 并 title 扫描，慢加载
        body='' 时 body <100 + 子串命中 → mark_deleted 永久跳过合法文章。
        只查 body 防此误杀。
        """
        snap = {"title": "评此账号已被屏蔽现象", "text": ""}
        assert saver._deleted_reason(snap) is None

    def test_threshold_99_hits(self):
        """len(body)=99 含关键词 → 命中（v7 阈值 100 边界）"""
        body = "此账号已被屏蔽" + "x" * (99 - len("此账号已被屏蔽"))
        assert len(body) == 99
        assert saver._deleted_reason({"text": body}) == "账号被屏蔽"

    def test_threshold_100_returns_none(self):
        """len(body)=100 含关键词 → 返回 None（v7 阈值 100 边界；capsys 无日志——纯函数）"""
        body = "此账号已被屏蔽" + "x" * (100 - len("此账号已被屏蔽"))
        assert len(body) == 100
        assert saver._deleted_reason({"text": body}) is None


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
        saver._POSSIBLE_MISS_SEEN.clear()  # v7 review #3：防 module-level 状态串扰
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

    def test_save_blocked_page_short_circuits_with_reason(self, isolated_vault, capsys):
        """屏蔽页 → ('deleted', None) + 日志含「账号被屏蔽」+ verify 被调用一次（顺序锁）"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "已屏蔽", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False) as mock_verify, \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}), \
             patch("ima_obsidian_saver.activate_browser") as mock_activate, \
             patch("ima_obsidian_saver.trigger_quick_clip") as mock_clip, \
             patch("ima_obsidian_saver.find_and_rename_in_vault") as mock_rename, \
             patch("ima_obsidian_saver.close_tab") as mock_close, \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        mock_clip.assert_not_called()
        mock_rename.assert_not_called()
        mock_activate.assert_not_called()
        mock_close.assert_called_once()
        mock_verify.assert_called_once()  # 锁死「verify 必先调用」顺序不变量
        captured = capsys.readouterr()
        assert "🗑️  账号被屏蔽，标记" in captured.out  # 全句匹配，防自取证日志误中

    def test_save_violation_page_short_circuits_with_reason(self, isolated_vault, capsys):
        """违规页（新文案）→ 日志含「违规不可查看」"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "违规", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此内容因违规无法查看"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        captured = capsys.readouterr()
        assert "🗑️  违规不可查看，标记" in captured.out

    def test_save_publisher_deleted_reason_in_stdout(self, isolated_vault, capsys):
        """发布者删除页 → 日志含「发布者删除」（回归保护 reason 文案）"""
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "已删", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.handle_verify_page", return_value=False), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信", "text": "该内容已被发布者删除"}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        captured = capsys.readouterr()
        assert "🗑️  发布者删除，标记" in captured.out

    def test_verify_precise_exclusion_e2e(self, isolated_vault):
        """端到端：不 mock handle_verify_page，验证 is_verify_page 前置排除真生效

        mock read_page_snapshot 返回屏蔽页 + mock click_confirm。
        若 is_verify_page 前置排除被破坏（删除 _deleted_reason 调用），屏蔽页会被
        误判为验证页 → handle_verify_page 调 click_confirm → call_count > 0 → 测试失败。
        mock verify 的集成测试（上面三个）无法发现此回归，故需此端到端用例。
        """
        vault, clip_dir = isolated_vault
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "屏蔽", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "微信公众平台", "text": "此账号已被屏蔽，内容无法查看"}), \
             patch("ima_obsidian_saver.click_confirm") as mock_click, \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault"), \
             patch("ima_obsidian_saver.time.sleep"):
            # 不 mock handle_verify_page —— 让它真跑，验证前置排除让 click_confirm 不被调
            result = saver.save_one_article(article, browser_config)

        assert result == ("deleted", None)
        assert mock_click.call_count == 0  # 屏蔽页不应触发 verify 重试

    def test_save_one_article_long_body_with_keyword_logs_miss(self, isolated_vault, capsys):
        """长 body(>=100) + 含 DELETED 关键词（不含 VERIFY）→ 漏检自证打 1 次（v7 §4.3）

        v7 review #2 修复：单次调用内只打 1 次（_deleted_reason 纯函数，自证在调用点）。
        PR #6 review #2：不 mock handle_verify_page——让 is_verify_page→_deleted_reason 路径
        真跑，锁住 v7（单次）vs v6（_log_possible_miss 在 _deleted_reason 内部时双打印）。
        PR #6 review #1：patch WAIT_CLIP_TOTAL=0 避免 while 循环 CPU-burn 25 秒。
        PR #6 review v3 #1：IMA_DEBUG_BODY_LEN=0 下也必须打（_log_possible_miss 移出门控）。
        PR #6 review v3 #4：日志含 url= / title= 定位信息。
        """
        saver._POSSIBLE_MISS_SEEN.clear()  # 防串扰
        vault, clip_dir = isolated_vault
        # body 含 DELETED 关键词但不含 VERIFY_KEYWORDS（避免 is_verify_page 走 True 分支）
        body = "此账号已被屏蔽" + "填充文本。" * 30  # len >= 100
        article = {"id": 1, "url": "https://mp.weixin.qq.com/s?__biz=T", "title": "T", "kb": "AI"}
        browser_config = {"app": "Chrome", "shortcut_mods": ["option", "shift"]}

        with patch("ima_obsidian_saver.extract_publish_date", return_value="260101"), \
             patch("ima_obsidian_saver.open_url"), \
             patch("ima_obsidian_saver.read_page_snapshot",
                   return_value={"title": "某文章", "text": body}), \
             patch("ima_obsidian_saver.activate_browser"), \
             patch("ima_obsidian_saver.trigger_quick_clip"), \
             patch("ima_obsidian_saver.close_tab"), \
             patch("ima_obsidian_saver.find_and_rename_in_vault", return_value=(False, None)), \
             patch("ima_obsidian_saver.WAIT_CLIP_TOTAL", 0), \
             patch.dict(os.environ, {"IMA_DEBUG_BODY_LEN": "0"}), \
             patch("ima_obsidian_saver.time.sleep"):
            # 不 mock handle_verify_page（PR #6 review #2）——让它真跑：
            #   is_verify_page(snap) → _deleted_reason(snap) → body>=阈值 → None
            #   → is_verify_page 继续 → body 不含 VERIFY → title != 微信公众平台 → False
            #   → handle_verify_page 第一次循环退出（不调 click_confirm）
            # 这样 _deleted_reason 被 is_verify_page + 调用点各调一次，若 _log_possible_miss
            # 在 _deleted_reason 内部（v6）会打 2 次 → count==2 → 测试失败（捕捉 v6 回归）。
            # PR #6 review v3 #1：强制 IMA_DEBUG_BODY_LEN=0，移门控后仍应打——若回退到
            # `if _debug_body_len_enabled:` 包裹的旧实现，则 count==0 → 测试失败。
            result = saver.save_one_article(article, browser_config)

        # 长违规页走 quick_clip 0 落盘 → ("failed", None)
        assert result == ("failed", None)
        captured = capsys.readouterr().out
        # 漏检自证只打 1 次（v7：_log_possible_miss 在调用点；v6 回滚会打 2 次 → 测试失败）
        assert captured.count("[疑似漏检自取证]") == 1
        # PR #6 review v3 #4：日志含 url= / title= 定位信息
        # PR #6 review v4 #2：精确值匹配（子串 "url=" 会被 url=None 的 bug 漏过）
        assert f"url={article['url']!r}" in captured
        assert f"title={article['title']!r}" in captured


class TestLogPossibleMiss:
    """_log_possible_miss: 漏检自取证（v7 §3.2）"""

    def setup_method(self):
        """每个测试前清空节流集合（防 module-level 状态串扰，v7 review #5）"""
        saver._POSSIBLE_MISS_SEEN.clear()

    def test_no_keyword_quiet(self, capsys):
        """body 不含 DELETED 关键词 → capsys 无输出"""
        saver._log_possible_miss("x" * 200)
        assert capsys.readouterr().out == ""

    def test_with_keyword_format(self, capsys):
        """含关键词 → 输出含 len/hits/body[:200]"""
        body = "此账号已被屏蔽" + "x" * 200
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert "[疑似漏检自取证]" in out
        assert "len(body)=" in out
        assert "此账号已被屏蔽" in out  # hits 含关键词

    def test_caps_body_at_200(self, capsys):
        """body 超 200 字 → 输出截断到 200（v7 review #5 修复）

        修正 brief 断言：原 `assert "yyy" not in out` 不可行——body[:200] 含
        连续 y 段，「yyy」必然在 out 中。改用独一无二尾缀：body 第 201+ 位
        放 ZTAIL，截断后该字符串必不在 out 中。
        """
        body = "此账号已被屏蔽" + "y" * 300 + "ZTAIL"  # 7 + 300 + 5 = 312 字
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert "ZTAIL" not in out  # 第 201+ 位的尾缀被截断
        assert out.count("y") < 300  # y 数远小于 300,证明被截断（宽松断言防 print 模板 y 干扰）

    def test_throttle_same_body(self, capsys):
        """同一 body 调 2 次 → 只打 1 次（v7 #6 节流）"""
        body = "此账号已被屏蔽" + "z" * 200
        saver._log_possible_miss(body)
        saver._log_possible_miss(body)
        out = capsys.readouterr().out
        assert out.count("[疑似漏检自取证]") == 1

    def test_different_body_both_print(self, capsys):
        """不同 body → 各打 1 次"""
        saver._log_possible_miss("此账号已被屏蔽" + "a" * 200)
        saver._log_possible_miss("该内容已被发布者删除" + "b" * 200)
        out = capsys.readouterr().out
        assert out.count("[疑似漏检自取证]") == 2

    def test_url_title_passed_through(self, capsys):
        """PR #6 review v3 #4：url/title 传参 → 日志含 url= / title= 定位信息"""
        body = "此账号已被屏蔽" + "x" * 200
        url = "https://mp.weixin.qq.com/s?__biz=Test"
        title = "某屏蔽文章标题"
        saver._log_possible_miss(body, url=url, title=title)
        out = capsys.readouterr().out
        assert f"url={url!r}" in out
        assert f"title={title!r}" in out

    def test_url_title_default_none(self, capsys):
        """PR #6 review v3 #4：默认值 None → 日志含 url=None title=None（兼容旧调用）"""
        saver._log_possible_miss("此账号已被屏蔽" + "x" * 200)
        out = capsys.readouterr().out
        assert "url=None" in out
        assert "title=None" in out
