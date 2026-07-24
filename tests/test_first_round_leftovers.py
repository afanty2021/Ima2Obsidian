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
        # from_id 同上（'from' 在 TRACKING_EXACT 精确匹配，不剥 from_id）
        (
            "https://example.com/article?from_id=aaa&x=1",
            "https://example.com/article?from_id=bbb&x=1",
        ),
        # scene_id / share_id 同理（'scene'/'share' 精确匹配，不剥 _id 变体）
        (
            "https://example.com/article?scene_id=111",
            "https://example.com/article?scene_id=222",
        ),
        (
            "https://example.com/article?share_id=aaa",
            "https://example.com/article?share_id=bbb",
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


class TestReclaimPreservesDbDateWhenContentHasNoDate:
    """回归 round-5 引入的生产 bug：content_date 为空串时 COALESCE 选中空串覆盖 DB

    场景：Clippings 文件正文没有 '*YYYY年M月D日*' 模式（Web Clipper 抓的页面
    日期格式不符/被裁剪），但 DB 行已有 published_date（saver 从 URL 抓到过）。
    旧 reclaim 把 content_date="" 喂给 COALESCE(?, published_date) →
    SQLite 中空串非 NULL → COALESCE('', '260625') 返回 '' → DB 真实日期被清空。

    根因：extract_date_from_content 失败时 return ""，应改为 return None。
    """

    def test_extract_date_from_content_returns_none_on_miss(self):
        """extract_date_from_content 无匹配时必须 return None，不能 return ''

        这是根因修复——任何喂给 COALESCE(?, published_date) 的路径都要求
        None（而非 ''）才能让 COALESCE 跳过、保留 DB 已有值。
        """
        from ima_obsidian_saver import extract_date_from_content
        result = extract_date_from_content("纯正文无日期标记")
        assert result is None, \
            f"无匹配时应返回 None 让 COALESCE 兜底；实际返回 {result!r}"

    def test_extract_date_from_content_returns_none_on_value_error(self):
        """正则匹配但 datetime() 抛 ValueError（如 *2026年13月45日*）时也必须 return None

        覆盖第二条失败出口：try/except ValueError 分支。
        若有人误改成 return '' 或删 try 让 ValueError 穿透崩溃，本测试拦住。
        """
        from ima_obsidian_saver import extract_date_from_content
        # 月=13、日=45 触发 datetime() ValueError
        result = extract_date_from_content("正文\n*2026年13月45日 10:00*\n")
        assert result is None, \
            f"ValueError 分支应返回 None；实际 {result!r}"

    def test_reclaim_preserves_db_date_when_content_has_no_date(
        self, temp_db, tmp_path, monkeypatch,
    ):
        """Clippings 文件无日期 + DB 已有 published_date → DB 必须保留原值

        回归测试：旧 round-5 实现下此场景 DB 日期被清空成 ''
        """
        init_database()
        # DB 行已有真实 published_date（saver 从 URL 抓到过）
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, "
            "obsidian_saved, published_date) VALUES (?,?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=T&mid=T&idx=1&sn=T",
             "测试文章A", "AI", "success", 0, "250625"),  # 已有真实日期
        )
        conn.commit()
        conn.close()

        # Clippings 文件正文无 *YYYY年M月D日* 标记
        vault = tmp_path / "Vault"
        vault.mkdir()
        (vault / "AI").mkdir()
        clip_dir = vault / "Clippings"
        clip_dir.mkdir()
        (clip_dir / "测试文章A.md").write_text(
            "# 标题\n\n纯正文，没有任何日期模式\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("reclaim_clippings.VAULT_DIR", vault)
        monkeypatch.setattr("reclaim_clippings.CLIPPINGS_DIR", clip_dir)
        monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
        monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
        monkeypatch.setattr("sys.argv", ["reclaim_clippings.py", "--apply"])

        import reclaim_clippings
        reclaim_clippings.main()

        conn = sqlite3.connect(temp_db)
        c = conn.cursor()
        c.execute("SELECT published_date, obsidian_saved FROM articles WHERE title='测试文章A'")
        pub, saved = c.fetchone()
        conn.close()
        # 关键不变式 1：DB 日期必须保留为 '250625'，不能被清空成 '' 或 NULL
        assert pub == "250625", (
            f"正文无日期时 DB 已有 published_date 必须保留；"
            f"期望 '250625'，实际 {pub!r}（如果 '' 或 None 说明被空串覆盖）"
        )
        # 关键不变式 2：reclaim 必须确实跑了（obsidian_saved=1），
        # 否则该测试无法区分"修复生效"与"reclaim 因标题匹配失败没碰 DB"
        assert saved == 1, (
            f"reclaim 应标记 obsidian_saved=1；实际 {saved}（若为 0 说明 reclaim "
            f"没匹配到该文章，DB 未被触碰，'保留原值' 只是没改而非修复生效）"
        )


# ==================== L4: get_stats 连接数回归锁 ====================

class TestGetStatsConnectionCount:
    """get_stats 用单连接做四条 SELECT（total / saved / unsaved / deleted）。

    注意：本测试只锁"连接数 == 1"这一结构属性，不真正验证 TOCTOU 安全性。
    SQLite 默认 isolation_level 不对 SELECT 触发 BEGIN，故单连接内多次 SELECT
    理论上可能看到不同快照（DEFERRED 模式下并发写入可见）。但本代码库是
    单用户 CLI 工具，无高并发写入场景，TOCTOU 风险纯理论。

    如果未来需要真正的并发一致性，应在 get_stats 里加显式 BEGIN/COMMIT，
    并补一个用线程/进程在 SELECT 间注入写的并发测试。
    """

    def test_get_stats_uses_single_connection(self, temp_db):
        """get_stats 只能开 1 个连接（结构回归锁，防止未来重构拆成多连接）"""
        from ima_obsidian_saver import get_stats
        from tests.test_db_connections import TrackingConnection

        init_database()
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved) "
            "VALUES (?,?,?,?,?)",
            ("https://mp.weixin.qq.com/s?__biz=T&mid=T&idx=1&sn=T", "T", "AI", "success", 0),
        )
        conn.commit()
        conn.close()

        real_connect = sqlite3.connect
        instances = []

        def tracking_connect(*args, **kwargs):
            real = real_connect(*args, **kwargs)
            wrapper = TrackingConnection(real)
            instances.append(wrapper)
            return wrapper

        with patch.object(sqlite3, "connect", tracking_connect):
            stats = get_stats()

        # 结构回归锁 1：只用 1 个连接（防未来重构拆成多连接）
        assert len(instances) == 1, \
            f"get_stats 应只用 1 个连接（结构回归锁）；实际开了 {len(instances)} 个"
        # 结构回归锁 2：返回值正确（零成本消除"调了不验证"的被动孪生）
        assert stats == {"total": 1, "saved": 0, "unsaved": 1, "deleted": 0}, \
            f"get_stats 返回值应为 1/0/1/0；实际 {stats}"
