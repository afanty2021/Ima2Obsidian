"""第三轮 #1/#2: incremental_update 入口守卫的真实测试

第二轮的测试手抄 main 逻辑、用截断 stdout 把 bug 藏住了。这一版直接调 main()，
并复刻真实 extractor 行为（守卫通过 → 后续步骤失败 → exit 1）。
"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ima_common import init_database, verify_urls_canonical
from ima_incremental_update import update_knowledge_base, main


def _insert_non_canonical(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status) VALUES (?,?,?,?)",
        ("https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B&scene=1",
         "T", "AI", "success"),
    )
    conn.commit()
    conn.close()


class TestMainEntryGuardCallsRealMain:
    """真正调 main()，覆盖入口预检的 init_database() 与 verify_urls_canonical() 顺序"""

    def test_main_aborts_on_unmigrated_db(self, temp_db, tmp_path, monkeypatch):
        """DB 含未规范 URL 时，main 应在入口预检 sys.exit(1)"""
        # 隔离 LOCK/LOG 文件
        monkeypatch.setattr("ima_incremental_update.LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr("ima_incremental_update.LOG_FILE", tmp_path / "l.log")
        init_database()
        _insert_non_canonical(temp_db)

        monkeypatch.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI"])
        monkeypatch.setattr("ima_incremental_update.ensure_daemon", lambda: True)

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1, "未迁移 DB 应 exit 1"

    def test_main_does_not_crash_on_fresh_db(self, temp_db, tmp_path, monkeypatch):
        """fresh DB（schema 不存在）上 main 必须先 init_database 再守卫，不能崩

        回归 #2：第二轮的入口预检漏了 init_database()，
        fresh DB 上 verify_urls_canonical 抛 'no such table: articles'。
        """
        monkeypatch.setattr("ima_incremental_update.LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr("ima_incremental_update.LOG_FILE", tmp_path / "l.log")
        # 不调 init_database，让 temp_db 是空文件
        assert not Path(temp_db).exists() or Path(temp_db).stat().st_size == 0

        monkeypatch.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI"])
        monkeypatch.setattr("ima_incremental_update.ensure_daemon", lambda: True)

        with patch("ima_incremental_update.update_knowledge_base",
                   return_value={"new": 0, "skipped": 0, "failed": 0}):
            try:
                main()
            except SystemExit as e:
                assert _schema_exists(temp_db), \
                    f"exit {e.code} 但 schema 未建——main 在 init_database 前就崩了"
            else:
                assert _schema_exists(temp_db), \
                    "main return 但 schema 未建——init_database 没跑"

    def test_main_succeeds_on_canonical_db(self, temp_db, tmp_path, monkeypatch):
        """已规范 DB 上 main 应能正常进入主循环（非 dry-run）"""
        monkeypatch.setattr("ima_incremental_update.LOCK_FILE", tmp_path / "l.lock")
        monkeypatch.setattr("ima_incremental_update.LOG_FILE", tmp_path / "l.log")
        init_database()
        monkeypatch.setattr("sys.argv", ["ima_incremental_update.py", "--kb", "AI"])
        monkeypatch.setattr("ima_incremental_update.ensure_daemon", lambda: True)

        with patch("ima_incremental_update.update_knowledge_base",
                   return_value={"new": 0, "skipped": 0, "failed": 0}):
            try:
                main()
            except SystemExit as e:
                assert e.code != 1, "已规范 DB 不应 exit 1"


def _schema_exists(db_path):
    """确认 articles 表已建"""
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute("SELECT 1 FROM articles LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


class TestExtractorFailureHandling:
    """extractor exit 1 时不应有任何基于 stdout 子串的"短路"判定

    回归 #1：第二轮的 stdout 检测子串 'URL 规范化自检' 同时命中守卫-通过行
    '✅ URL 规范化自检通过'，导致 extractor 守卫通过后任何 exit 1（daemon 挂、
    窗口丢、AX<100）都被误判为守卫触发 → main 跳过剩余 KB → 静默漏存。
    """

    def test_extractor_exit1_with_guard_pass_line_does_not_abort(self, temp_db, monkeypatch):
        """真实 extractor 行为：守卫通过必打 '✅ URL 规范化自检通过'，之后若
        daemon 挂/AX<100 也会 exit 1。绝不能因此跳过剩余 KB。"""
        init_database()

        fake_result = MagicMock()
        fake_result.returncode = 1
        # 这是真实 extractor 的典型 stdout：守卫通过 + 后续失败
        fake_result.stdout = (
            "✅ 数据库: ima_articles.db (已有 100 篇)\n"
            "✅ URL 规范化自检通过\n"
            "❌ cua-driver daemon 未运行\n"
        )
        fake_result.stderr = ""

        with patch("ima_incremental_update.subprocess.run", return_value=fake_result), \
             patch("ima_incremental_update.activate_ima"), \
             patch("ima_incremental_update.ensure_ima_ready", return_value=True), \
             patch("ima_incremental_update.get_ima_main_window",
                   return_value={"pid": 1, "window_id": 1, "bounds": {}}):
            stats = update_knowledge_base("AI", dry_run=False)

        # 关键不变式：不能有 abort_remaining（它只该被守卫失败触发，
        # 而入口预检已先拦截真正的守卫失败，extractor 子进程的守卫总通过）
        assert not stats.get("abort_remaining"), \
            f"守卫已通过的失败被误判为守卫触发 → 会静默跳过剩余 KB；stats={stats}"
        assert stats["failed"] == 1, "应当作为普通失败计数"
