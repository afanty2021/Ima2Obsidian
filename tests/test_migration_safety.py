"""#2: normalize_url 改格式后的部署安全网

风险：新格式会让 1281 行旧 URL 在下次提取时被当成新文章重复入库。
缓解：
  (a) migrate_normalize_urls 必须正确把所有 URL 规范化（幂等、不丢数据）
  (b) 加 verify_urls_canonical() 守卫，提取前自检；不通过则明确报错
  (c) extractor 启动时调用守卫，未迁移则终止并提示
"""
import sqlite3
from pathlib import Path

import pytest

from ima_ax_extractor import normalize_url
from ima_common import init_database


def _insert_articles(db_path, urls):
    init_database()  # 确保 schema 存在
    conn = sqlite3.connect(db_path)
    for url in urls:
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status) VALUES (?,?,?,?)",
            (url, "t", "AI", "success"),
        )
    conn.commit()
    conn.close()


def _all_urls(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT id, url FROM articles ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return rows


class TestMigrateNormalizeUrls:
    """migrate_normalize_urls 必须把所有 URL 规范化，幂等且不丢数据"""

    def test_migrate_canonicalizes_all_urls(self, temp_db, capsys):
        """运行 migrate 后，所有 URL 必须 == normalize_url(url)"""
        # 插入若干"旧格式"URL（参数未排序）
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1&chksm=abc&scene=1",
            "https://mp.weixin.qq.com/s?mid=M2&sn=S2&__biz=B2&idx=1",
            "https://mp.weixin.qq.com/s/shortid1?utm_source=x",
            "https://mp.weixin.qq.com/s?__biz=B3&mid=M3&idx=1&sn=S3",  # 已规范
        ])
        # 触发 migrate
        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        captured = capsys.readouterr()

        for aid, url in _all_urls(temp_db):
            assert url == normalize_url(url), \
                f"id={aid} URL 未规范: {url!r} -> {normalize_url(url)!r}"

    def test_migrate_idempotent(self, temp_db, capsys):
        """migrate 跑两次，第二次必须无变更"""
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1",
            "https://mp.weixin.qq.com/s?__biz=B2&mid=M2&idx=1&sn=S2&scene=1",
        ])
        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        capsys.readouterr()

        # 第二次跑
        migrate_urls()
        out = capsys.readouterr().out
        assert "无需迁移" in out or "需要迁移: 0 条" in out, \
            f"第二次 migrate 应是无操作: {out!r}"

    def test_migrate_handles_duplicates_without_data_loss(self, temp_db, capsys):
        """两条不同 URL 规范化后碰撞（实际同一篇文章两份），migrate 应保留一条"""
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S&scene=1",  # 旧格式
            "https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B",  # 参数乱序，规范化后相同
        ])
        from migrate_normalize_urls import migrate_urls
        migrate_urls()

        rows = _all_urls(temp_db)
        urls = [u for _, u in rows]
        # 应只剩一条（规范化后是同一个 URL，UNIQUE 约束触发 DELETE）
        assert len(rows) == 1, f"应去重到 1 条，实际 {len(rows)}: {rows}"
        assert urls[0] == normalize_url(urls[0])

    def test_migrate_preserves_other_columns(self, temp_db, capsys):
        """migrate 不应丢失 title/kb/obsidian_saved 等列"""
        init_database()
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved, published_date) "
            "VALUES (?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B", "标题X", "Invest", "success", 1, "250101"),
        )
        conn.commit()
        conn.close()

        from migrate_normalize_urls import migrate_urls
        migrate_urls()

        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT title, knowledge_base, status, obsidian_saved, published_date FROM articles")
        row = c.fetchone()
        conn.close()
        assert row == ("标题X", "Invest", "success", 1, "250101"), f"列值丢失: {row}"


class TestVerifyUrlsCanonicalGuard:
    """守卫函数 verify_urls_canonical：检测未规范化的 URL，提取前自检"""

    def test_returns_empty_when_all_canonical(self, temp_db):
        init_database()
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?__biz=B1&mid=M1&idx=1&sn=S1",  # 已规范（排序后仍是这顺序）
            "https://mp.weixin.qq.com/s/shortid1",
        ])
        from ima_common import verify_urls_canonical
        non_canonical = verify_urls_canonical()
        assert non_canonical == [], f"应无未规范 URL，实际: {non_canonical}"

    def test_detects_non_canonical_urls(self, temp_db):
        init_database()
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1",  # 乱序
            "https://mp.weixin.qq.com/s/shortid1?utm_source=x",  # 短格式带 utm
        ])
        from ima_common import verify_urls_canonical
        non_canonical = verify_urls_canonical()
        assert len(non_canonical) == 2, f"应检出 2 行未规范，实际: {non_canonical}"
        # 每条都包含 (id, current_url, canonical_url) 三元组
        for entry in non_canonical:
            assert len(entry) == 3
            aid, cur, canon = entry
            assert cur != canon

    def test_after_migrate_returns_empty(self, temp_db, capsys):
        """先 migrate 再 verify，应为空"""
        init_database()
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1&scene=1",
            "https://mp.weixin.qq.com/s/shortid1?utm_source=x",
        ])
        from migrate_normalize_urls import migrate_urls
        from ima_common import verify_urls_canonical
        migrate_urls()
        capsys.readouterr()

        non_canonical = verify_urls_canonical()
        assert non_canonical == [], f"migrate 后仍检出未规范 URL: {non_canonical}"


class TestMigrateMetadataPreservation:
    """migrate 在去重 DELETE 时必须先合并 obsidian_saved/obsidian_saved_at/published_date

    旧实现：UPDATE 失败 → 直接 DELETE 重复行，可能把"已保存"的行删掉，
    留下未规范的"未保存"行 → vault 里 .md 已存在但 DB 标 unsaved → 永久漏存。
    """

    def test_preserves_saved_state_when_canonical_row_is_unsaved(self, temp_db, capsys):
        """场景：旧 URL 行已保存，规范 URL 行未保存。
        migrate 应让保留的（规范 URL）行带上 obsidian_saved=1。"""
        init_database()
        conn = sqlite3.connect(temp_db)
        # 旧格式行：已保存到 vault
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, "
            "obsidian_saved, obsidian_saved_at, published_date) VALUES (?,?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B",  # 乱序，未规范
             "T", "AI", "success", 1, "2025-01-01T00:00:00", "250101"),
        )
        # 规范 URL 行：未保存（url 已是规范形式）
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, "
            "obsidian_saved, obsidian_saved_at, published_date) VALUES (?,?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S",  # 已规范
             "T-dup", "AI", "success", 0, None, None),
        )
        conn.commit()
        conn.close()

        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        capsys.readouterr()

        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM articles")
        assert c.fetchone()[0] == 1, "应去重到 1 行"
        c.execute(
            "SELECT obsidian_saved, obsidian_saved_at, published_date FROM articles"
        )
        saved, saved_at, pub = c.fetchone()
        conn.close()
        assert saved == 1, f"保留行应继承 obsidian_saved=1（避免回退为未保存），实际 {saved}"
        assert saved_at == "2025-01-01T00:00:00", f"应继承 obsidian_saved_at，实际 {saved_at!r}"
        assert pub == "250101", f"应继承 published_date，实际 {pub!r}"

    def test_keeper_metadata_takes_precedence_when_both_have_it(self, temp_db, capsys):
        """双方都有元数据时，保留行（已存在规范 URL）的元数据优先"""
        init_database()
        conn = sqlite3.connect(temp_db)
        # 旧格式行（要被 DELETE 的）
        conn.execute(
            "INSERT INTO articles (url, title, status, "
            "obsidian_saved, obsidian_saved_at, published_date) VALUES (?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?sn=S&__biz=B&mid=M&idx=1",
             "T", "success", 1, "2024-12-31T00:00:00", "241231"),
        )
        # 规范 URL 行（保留方，元数据更权威）
        conn.execute(
            "INSERT INTO articles (url, title, status, "
            "obsidian_saved, obsidian_saved_at, published_date) VALUES (?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S",
             "T-dup", "success", 1, "2025-06-01T00:00:00", "250601"),
        )
        conn.commit()
        conn.close()

        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        capsys.readouterr()

        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT obsidian_saved_at, published_date FROM articles")
        saved_at, pub = c.fetchone()
        conn.close()
        # 保留行的值应胜出
        assert saved_at == "2025-06-01T00:00:00", f"保留行元数据应优先，实际 {saved_at!r}"
        assert pub == "250601", f"保留行 published_date 应优先，实际 {pub!r}"

    def test_migrate_reports_merged_separately_not_as_error(self, temp_db, capsys):
        """成功的合并去重不应被算作 error_count（避免汇总误导运维）

        回归 #6：旧实现 merged 行 error_count += 1，汇总打 '失败: 1 条'，
        让运维以为有真失败。应单列 merged_count。
        """
        init_database()
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, status, obsidian_saved, published_date) "
            "VALUES (?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B",
             "T", "success", 1, "250101"),
        )
        conn.execute(
            "INSERT INTO articles (url, title, status, obsidian_saved, published_date) "
            "VALUES (?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S",
             "T-dup", "success", 0, None),
        )
        conn.commit()
        conn.close()

        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        out = capsys.readouterr().out

        # 必须有合并计数；不应报告失败
        assert "合并去重: 1" in out, f"应单列合并计数，实际输出: {out!r}"
        # 关键不变式：成功的合并去重不应被算作失败
        # 'error_count' 在新代码里是死代码（永不自增），所以 '失败: N 条' 永不打印
        assert "失败:" not in out or "失败: 0 条" in out, \
            f"合并成功不应计入失败，实际输出: {out!r}"

    def test_migrate_locked_db_rolls_back_cleanly(self, temp_db, capsys, monkeypatch):
        """'database is locked' 命中迁移中第 N 条 UPDATE 时，前 N-1 条已成功的 UPDATE 必须被回滚

        回归 #2（round-3 测试孪生）：旧测试在第一条 UPDATE 就抛错，0 条真正
        执行，'5 行原始 URL 保留' 只是"没改过"而非"回滚了"。要真正测回滚：
        让前几条 UPDATE 成功（落库），第 4 条抛错，验证前 3 条 URL 被还原。
        """
        init_database()
        conn = sqlite3.connect(temp_db)
        # 插 5 行需要迁移的旧格式 URL
        original_urls = []
        for i in range(5):
            u = f"https://mp.weixin.qq.com/s?sn=S{i}&idx=1&mid=M{i}&__biz=B{i}"
            conn.execute(
                "INSERT INTO articles (url, title, status) VALUES (?,?,?)",
                (u, f"T{i}", "success"),
            )
            original_urls.append(u)
        conn.commit()
        conn.close()

        # 用 TrackingConnection 让第 4 次 UPDATE 抛错（前 3 次成功）
        from tests.test_db_connections import TrackingConnection
        real_connect = sqlite3.connect

        def faulty_connect(*args, **kwargs):
            real = real_connect(*args, **kwargs)
            wrapper = TrackingConnection(real)
            wrapper.set_fail_on_nth_update(4)
            return wrapper

        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", faulty_connect)
            from migrate_normalize_urls import migrate_urls
            with pytest.raises(sqlite3.OperationalError):
                migrate_urls()

        out = capsys.readouterr().out
        assert "已回滚" in out or "回滚" in out, \
            f"locked DB 中断时应明确提示已回滚，实际: {out!r}"

        # 关键不变式：5 行原始 URL 全部保留（前 3 条 UPDATE 已成功但必须被回滚还原）
        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT url FROM articles ORDER BY url")
        urls_after = [r[0] for r in c.fetchall()]
        conn.close()

        assert len(urls_after) == 5, f"行数应保留 5 行，实际: {len(urls_after)}"
        # 关键：前 3 条 UPDATE 已成功（URL 已改成规范形式）但必须被回滚还原
        # 若回滚失效，会留下部分已规范的 URL——下次 migrate 时它们无需迁移，
        # 但第 4 条以后的 URL 仍是旧格式，导致半完成状态
        for original, current in zip(sorted(original_urls), urls_after):
            assert current == original, (
                f"已成功的 UPDATE 必须被回滚还原：原 {original!r}，"
                f"当前 {current!r}（这条若通过说明回滚失效，半完成状态被保留）"
            )


class TestExtractorGuard:
    """extractor 启动时必须自检 URL 规范化，未迁移则非零退出（避免重复入库）"""

    def test_extractor_aborts_when_urls_not_canonical(self, temp_db, monkeypatch, capsys):
        init_database()
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1&scene=1",  # 乱序，未规范
        ])

        # 触发 extractor 的 main 路径前段（init_database + verify_urls_canonical 检查）
        # 直接调用 verify_urls_canonical 模拟 extractor 的检查逻辑
        from ima_common import verify_urls_canonical
        non_canonical = verify_urls_canonical()
        assert len(non_canonical) >= 1, "应当检出未规范 URL"

        # 验证 extractor 的实际行为：在未迁移 DB 上 main 应 sys.exit(1)
        import asyncio
        from unittest.mock import patch, AsyncMock
        with patch("sys.argv", ["ima_ax_extractor.py", "--src", "AI"]), \
             patch("ima_ax_extractor.is_daemon_running", return_value=True), \
             pytest.raises(SystemExit) as exc_info:
            asyncio.run(__import__("ima_ax_extractor").main())
        assert exc_info.value.code == 1, \
            f"extractor 在未迁移 DB 上应非零退出，实际 exit={exc_info.value.code}"

    def test_extractor_proceeds_after_migrate(self, temp_db, capsys, monkeypatch):
        """migrate 后 extractor 不应在 verify 阶段退出"""
        init_database()
        _insert_articles(temp_db, [
            "https://mp.weixin.qq.com/s?sn=S1&idx=1&mid=M1&__biz=B1&scene=1",
        ])
        from migrate_normalize_urls import migrate_urls
        migrate_urls()
        capsys.readouterr()

        from ima_common import verify_urls_canonical
        non_canonical = verify_urls_canonical()
        assert non_canonical == [], "migrate 后 verify 应为空，extractor 不应在此处退出"
