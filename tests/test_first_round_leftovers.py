"""第一轮遗留项修复测试

覆盖 4 个遗留缺陷：
  L1: run_cua_call 只捕 RuntimeError，subprocess.TimeoutExpired 穿透崩提取
  L2: normalize_url 通用分支 'ref'/'source' 前缀碰撞（ref_id/source_id 被误剥）
  L3: mark_saved 的 COALESCE(?, published_date) 与 reclaim 的 COALESCE(published_date, ?) 优先级相反
  L4: get_stats 三条 SELECT TOCTOU（不在同一事务，并发写时不一致）
"""
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from ima_common import init_database
from ima_ax_extractor import run_cua_call, normalize_url
from ima_obsidian_saver import mark_saved


# ==================== L1: run_cua_call 必须捕获 TimeoutExpired ====================

class TestRunCuaCallTimeoutException:
    """run_cua 内部 subprocess.run(..., timeout=...) 抛 TimeoutExpired，
    run_cua_call 只捕 RuntimeError 时会穿透崩调用栈（extractor 的 get_window_state 等）
    """

    def test_timeout_returns_none_not_crash(self):
        """cua-driver 超时时，run_cua_call 应返回 None（让调用方优雅重试/降级），
        而非让 TimeoutExpired 穿透到 extractor main 把整个提取流程崩掉。
        """
        with patch("ima_ax_extractor.run_cua",
                   side_effect=subprocess.TimeoutExpired(cmd="cua-driver", timeout=30)):
            result = run_cua_call("get_window_state", {"pid": 1, "window_id": 1})
        assert result is None, \
            f"TimeoutExpired 应被捕获并返回 None，让调用方优雅降级；实际: {result!r}"

    def test_timeout_does_not_print_full_traceback(self, capsys):
        """超时应像 RuntimeError 一样打印简短警告，而非让 Python 解释器打顶层 traceback"""
        with patch("ima_ax_extractor.run_cua",
                   side_effect=subprocess.TimeoutExpired(cmd="cua-driver", timeout=30)):
            run_cua_call("scroll", {"pid": 1, "window_id": 1, "direction": "down"})
        captured = capsys.readouterr()
        assert "cua-driver" in captured.out and "失败" in captured.out, \
            f"应有简短失败提示，实际: {captured.out!r}"

    def test_runtime_error_still_caught(self):
        """回归：RuntimeError 仍应被捕获（不能因为加了 TimeoutExpired 而漏掉）"""
        with patch("ima_ax_extractor.run_cua", side_effect=RuntimeError("exit 1")):
            assert run_cua_call("click", {"x": 1}) is None


# ==================== L2: normalize_url 不能误剥 ref_id / source_id ====================

class TestNormalizeUrlPreservesContentParams:
    """通用分支的 'ref'/'source' 前缀匹配会误剥 ref_id、source_id 等内容参数，
    导致两条不同 URL 折叠到同一规范形式 → UNIQUE 约束触发误判重复 → 漏存。
    """

    @pytest.mark.parametrize("url_a,url_b", [
        # ref_id 是内容参数，不应被剥（两条 ref_id 不同的 URL 必须保持不同）
        (
            "https://example.com/article?ref_id=111&id=1",
            "https://example.com/article?ref_id=222&id=1",
        ),
        # source_id 同上
        (
            "https://example.com/article?source_id=aaa&x=1",
            "https://example.com/article?source_id=bbb&x=1",
        ),
        # ref_id 与 source_id 同时存在，互不剥
        (
            "https://example.com/article?ref_id=1&source_id=2",
            "https://example.com/article?ref_id=3&source_id=4",
        ),
    ])
    def test_distinct_urls_remain_distinct(self, url_a, url_b):
        """两条不同内容 URL 规范化后必须仍不同（不折叠）"""
        assert normalize_url(url_a) != normalize_url(url_b), \
            f"两条不同 URL 被折叠到同一规范形式：{normalize_url(url_a)!r}"

    def test_tracking_only_ref_still_stripped(self):
        """纯追踪参数 ref=share / source=wechat 仍应被剥（保留向后兼容）"""
        assert normalize_url("https://example.com/a?ref=share&id=1") \
            == normalize_url("https://example.com/a?id=1")
        assert normalize_url("https://example.com/a?source=wechat&id=1") \
            == normalize_url("https://example.com/a?id=1")

    def test_idempotent_after_fix(self):
        """修复后仍保持幂等性"""
        urls = [
            "https://example.com/a?ref_id=1&utm_source=x",
            "https://example.com/b?source_id=2&from=app",
            "https://mp.weixin.qq.com/s?__biz=B&mid=M&idx=1&sn=S",
        ]
        for u in urls:
            assert normalize_url(u) == normalize_url(normalize_url(u))


# ==================== L3: mark_saved 与 reclaim 的 COALESCE 优先级必须一致 ====================

class TestMarkSavedCoalescePriority:
    """两个写者的 published_date COALESCE 优先级必须语义一致：
    旧 mark_saved 用 COALESCE(?, published_date)（新值优先，DB 兜底），
    旧 reclaim 用 COALESCE(published_date, ?)（DB 优先，新值兜底）。
    场景：saver 第一次跑时 extract_publish_date 正则失配降级为"今天"，
    写入"今天"作为 published_date；下次 saver 重跑同一篇（极少，但可能），
    extract_publish_date 这次命中正则得到真实日期 →
    - 旧 mark_saved：用真实日期覆盖"今天"（数据纠正，正确）
    - 但若 saver 第二次也降级（连续降级），用"今天"覆盖上次的"今天"，
      仍是错的——优先级对新值/旧值都有失败模式
    真实问题：旧 mark_saved 的 COALESCE(?, published_date) 在调用方传 None
    （saver 第一轮的 mark_saved(article["id"]) 未传 date）时，把 DB 已有
    真实日期保留——这是好事。但 reclaim 的 COALESCE(published_date, ?)
    在 saver 之后跑时，会保留 saver 写入的"今天"，不换成文件正文的真实日期。
    一致性要求：两个写者用同一种 COALESCE 语义。
    """

    def test_mark_saved_and_reclaim_use_same_coalesce_form(self, temp_db):
        """两个 UPDATE SQL 必须用同一形式的 COALESCE（顺序一致）"""
        import reclaim_clippings
        # 直接读源码检查 SQL 形式（结构化测试，避免复杂集成）
        import inspect
        mark_saved_src = inspect.getsource(mark_saved)
        reclaim_update_src = None
        # reclaim 在 main() 函数体内的 UPDATE 语句，从源码 grep
        reclaim_src = inspect.getsource(reclaim_clippings)
        # 检查两者的 published_date COALESCE 形式
        # 期望两者都是 COALESCE(?, published_date)（新值优先，DB 兜底）
        # 或两者都是 COALESCE(published_date, ?)（DB 优先，新值兜底）
        assert "COALESCE(?, published_date)" in mark_saved_src, \
            f"mark_saved SQL 形式变了；现源码:\n{mark_saved_src}"
        # reclaim 的 UPDATE 也应该用相同形式
        assert "COALESCE(?, published_date)" in reclaim_src, \
            "reclaim 的 UPDATE 应与 mark_saved 用同一 COALESCE 形式（?, published_date）"

    def test_reclaim_overwrites_saver_fallback_date_with_real(self, temp_db, tmp_path, monkeypatch):
        """集成验证：saver 写入降级日期 → reclaim 从文件正文读真实日期 →
        新值应覆盖旧值（COALESCE(?, published_date) 语义）

        场景来自实际 bug：saver 第一轮降级为今天写入 DB，
        reclaim 后续从 Clippings 正文提取到真实日期，应纠正 DB。
        """
        init_database()
        # 1) 模拟 saver 已写入：obsidian_saved=0（让 reclaim 能 SELECT 到），
        #    published_date=降级值（今天）
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, "
            "obsidian_saved, published_date) VALUES (?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=T&mid=T&idx=1&sn=T",
             "测试文章A", "AI", "success", 0, "260701"),  # 降级日期
        )
        conn.commit()
        conn.close()

        # 2) 模拟 reclaim 跑：vault 含 .md，正文带真实日期 250304
        from ima_obsidian_saver import VAULT_DIR, CLIPPINGS_DIR
        vault = tmp_path / "Vault"
        vault.mkdir()
        (vault / "AI").mkdir()
        clip_dir = vault / "Clippings"
        clip_dir.mkdir()
        (clip_dir / "测试文章A.md").write_text(
            "正文\n*2025年3月4日 10:00*\n", encoding="utf-8"
        )
        monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
        monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
        monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

        import reclaim_clippings
        reclaim_clippings.main()

        # 3) 关键不变式：reclaim 应用真实日期 250304 覆盖 saver 的降级日期 260701
        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT published_date FROM articles WHERE title='测试文章A'")
        pub = c.fetchone()[0]
        conn.close()
        assert pub == "250304", \
            f"reclaim 应让真实日期覆盖 saver 降级值；DB 应为 250304，实际 {pub!r}"


# ==================== L4: get_stats 三条 SELECT 必须事务一致 ====================

class TestGetStatsTransactional:
    """get_stats 跑三条独立 SELECT（total / saved / unsaved），
    不在同一事务/连接里，并发写时三条可能看到不同状态 → 不变量不成立：
    saved + unsaved 可能 > total，或 unsaved 与 get_unsaved_articles 不一致。

    修复：把三条 SELECT 放到同一连接的同一 BEGIN/COMMIT 内（isolation_level
    或显式 BEGIN），保证读到一致的快照。
    """

    def test_three_selects_use_same_connection(self, temp_db):
        """结构验证：get_stats 不能开三个独立连接做三次 SELECT"""
        from ima_obsidian_saver import get_stats
        from unittest.mock import MagicMock
        # 用 TrackingConnection 计数连接打开次数
        from tests.test_db_connections import TrackingConnection

        # 先建 schema + 插数据
        init_database()
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
            "VALUES (?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=T&mid=T&idx=1&sn=T", "T", "AI", "success", 0),
        )
        conn.commit()
        conn.close()

        # 注入 tracking wrapper，统计 connect 调用次数
        real_connect = sqlite3.connect
        instances = []

        def tracking_connect(*args, **kwargs):
            real = real_connect(*args, **kwargs)
            wrapper = TrackingConnection(real)
            instances.append(wrapper)
            return wrapper

        with patch.object(sqlite3, "connect", tracking_connect):
            stats = get_stats()

        # 关键不变式：get_stats 只能开 1 个连接，所有 SELECT 共享同一事务快照
        assert len(instances) == 1, \
            f"get_stats 必须用单一连接保证事务一致，实际开了 {len(instances)} 个"

    def test_invariants_hold_under_conceptual_concurrent_write(self, temp_db):
        """三条 SELECT 必须满足不变式：saved + unsaved == total（status+url 过滤后）

        在单线程里这条永远是 True（SQLite 默认隔离）。
        但若 get_stats 跨连接读，理论上可能读到中间状态。
        这条测试主要验证逻辑不变式仍成立，配合上一条结构测试。
        """
        init_database()
        conn = sqlite3.connect(temp_db)
        # 5 行：3 success mp（1 saved + 2 unsaved）、1 failed、1 非 mp
        for i, (url, status, saved) in enumerate([
            ("https://mp.weixin.qq.com/s?__biz=A&mid=A&idx=1&sn=A", "success", 1),
            ("https://mp.weixin.qq.com/s?__biz=B&mid=B&idx=1&sn=B", "success", 0),
            ("https://mp.weixin.qq.com/s?__biz=C&mid=C&idx=1&sn=C", "success", 0),
            ("https://mp.weixin.qq.com/s?__biz=D&mid=D&idx=1&sn=D", "failed", 0),
            ("https://example.com/x", "success", 0),
        ]):
            conn.execute(
                "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
                "VALUES (?,?,?,?,?)",
                (url, f"T{i}", "AI", status, saved),
            )
        conn.commit()
        conn.close()

        from ima_obsidian_saver import get_stats
        stats = get_stats()
        # 三条 SELECT 必须满足：saved + unsaved == total（在 status+url 过滤口径内）
        assert stats["saved"] + stats["unsaved"] == stats["total"], (
            f"事务不变式失败：saved({stats['saved']}) + unsaved({stats['unsaved']}) "
            f"!= total({stats['total']})"
        )
        assert stats["saved"] == 1
        assert stats["unsaved"] == 2
        assert stats["total"] == 3
