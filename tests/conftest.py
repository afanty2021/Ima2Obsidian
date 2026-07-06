"""
共享 pytest fixtures

关键策略：
- 每个测试用独立临时 DB 文件，避免污染真实 ima_articles.db
- monkeypatch DB_FILE 在模块加载后就已绑定，需 patch 模块属性
- subprocess 调用默认 mock，避免触发 cua-driver / osascript
"""
import importlib
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """提供一个干净临时 DB，并 patch 所有相关模块的 DB_FILE"""
    db_path = tmp_path / "test_articles.db"

    # patch 每个用到 DB_FILE 的模块的模块级属性
    for mod_name in ("ima_common", "ima_ax_extractor", "ima_obsidian_saver",
                     "ima_incremental_update", "reclaim_clippings",
                     "migrate_normalize_urls"):
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "DB_FILE"):
            monkeypatch.setattr(mod, "DB_FILE", db_path)

    yield db_path


@pytest.fixture
def seeded_db(temp_db):
    """初始化 schema 并插入若干测试文章"""
    from ima_common import init_database
    init_database()

    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    # 插入 3 篇：1 已保存 + 2 未保存
    test_rows = [
        # (url, title, kb, status, obsidian_saved, obsidian_saved_at, published_date)
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M1&idx=1&sn=S1", "已保存文章", "AI", "success", 1, "2026-01-01T10:00:00", "260101"),
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M2&idx=1&sn=S2", "未保存文章A", "AI", "success", 0, None, None),
        ("https://mp.weixin.qq.com/s?__biz=B&mid=M3&idx=1&sn=S3", "未保存文章B", "Invest", "success", 0, None, None),
        # 非 success / 非 mp.weixin 不应被统计
        ("https://example.com/x", "无效状态", "AI", "failed", 0, None, None),
    ]
    c.executemany(
        "INSERT INTO articles (url, title, knowledge_base, status, obsidian_saved, obsidian_saved_at, published_date) "
        "VALUES (?,?,?,?,?,?,?)",
        test_rows,
    )
    conn.commit()
    conn.close()
    return temp_db
