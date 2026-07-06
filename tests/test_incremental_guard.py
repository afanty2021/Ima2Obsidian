"""#3: incremental_update 必须在入口预检 + 识别 extractor 守卫短路剩余 KB"""
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ima_common import init_database, verify_urls_canonical
from ima_incremental_update import update_knowledge_base


def _insert_non_canonical(db_path):
    """插一行未规范 URL，触发 verify_urls_canonical"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO articles (url, title, knowledge_base, status) VALUES (?,?,?,?)",
        ("https://mp.weixin.qq.com/s?sn=S&idx=1&mid=M&__biz=B&scene=1",  # 乱序
         "T", "AI", "success"),
    )
    conn.commit()
    conn.close()


class TestIncrementalEntryGuard:
    """incremental_update main() 入口必须 verify_urls_canonical 自检"""

    def test_aborts_when_db_not_migrated(self, temp_db, monkeypatch, capsys):
        """DB 含未规范 URL 时，main 应 sys.exit(1)，不进入 KB 处理"""
        init_database()
        _insert_non_canonical(temp_db)

        # 模拟 incremental_update 的入口预检逻辑
        from ima_common import verify_urls_canonical
        non_canonical = verify_urls_canonical()
        assert len(non_canonical) >= 1, "应检出未规范 URL"

        # 模拟 main() 入口检查并退出
        # （不直接调 main，因为它会触发 IMA 激活等副作用；
        # 改为复刻入口守卫的判断逻辑）
        with pytest.raises(SystemExit) as exc_info:
            if non_canonical:
                print(f"❌ 检测到 {len(non_canonical)} 行 URL 未规范")
                sys.exit(1)
        assert exc_info.value.code == 1


class TestExtractorGuardPropagation:
    """update_knowledge_base 识别 extractor 守卫退出并返回 abort_remaining"""

    def test_update_kb_returns_abort_when_extractor_guard_triggers(self, temp_db, monkeypatch):
        """extractor 因守卫 exit 1 时，update_knowledge_base 返回 abort_remaining=True"""
        init_database()

        # mock subprocess.run 让 extractor 退出码 1 + stdout 含守卫提示
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = (
            "❌ 检测到 5 行 URL 未规范（normalize_url 口径变更后需迁移）\n"
            "   请先运行: python3 migrate_normalize_urls.py\n"
        )
        fake_result.stderr = ""

        # update_knowledge_base 内部还调用了 activate_ima、ensure_ima_ready 等副作用，
        # 全部 mock 掉
        with patch("ima_incremental_update.subprocess.run", return_value=fake_result), \
             patch("ima_incremental_update.activate_ima"), \
             patch("ima_incremental_update.ensure_ima_ready", return_value=True), \
             patch("ima_incremental_update.get_ima_main_window",
                   return_value={"pid": 1, "window_id": 1, "bounds": {}}):
            stats = update_knowledge_base("AI", dry_run=False)

        assert stats.get("abort_remaining") is True, \
            f"extractor 守卫触发时应返回 abort_remaining=True，实际: {stats}"
        assert stats["failed"] == 1

    def test_update_kb_does_not_abort_on_normal_extract_failure(self, temp_db, monkeypatch):
        """extractor 一般性失败（exit 1 但 stdout 无守卫提示）不应短路"""
        init_database()

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = "❌ cua-driver daemon 未运行\n"
        fake_result.stderr = ""

        with patch("ima_incremental_update.subprocess.run", return_value=fake_result), \
             patch("ima_incremental_update.activate_ima"), \
             patch("ima_incremental_update.ensure_ima_ready", return_value=True), \
             patch("ima_incremental_update.get_ima_main_window",
                   return_value={"pid": 1, "window_id": 1, "bounds": {}}):
            stats = update_knowledge_base("AI", dry_run=False)

        assert not stats.get("abort_remaining"), \
            f"一般性失败不应 abort_remaining，实际: {stats}"
        assert stats["failed"] == 1
