"""F1: mark_saved 必须保留已存在的 published_date，并接受新日期"""
import sqlite3
from datetime import datetime

import pytest

from ima_obsidian_saver import mark_saved


def fetch_row(db_path, article_id):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("SELECT obsidian_saved, obsidian_saved_at, published_date FROM articles WHERE id=?", (article_id,))
    row = c.fetchone()
    conn.close()
    return row


def test_mark_saved_preserves_existing_published_date(seeded_db):
    """已存在 published_date 时，不带 date 调用 mark_saved 不应覆盖"""
    # 先手工写入一个 published_date
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE articles SET published_date='260101' WHERE id=2")
    conn.commit()
    conn.close()

    # main() 现有调用方式：只传 id，不传 date
    mark_saved(2)

    row = fetch_row(seeded_db, 2)
    assert row[0] == 1, "obsidian_saved 应为 1"
    assert row[1] is not None, "obsidian_saved_at 应已写入"
    assert row[2] == "260101", f"published_date 应保留为 260101，实际: {row[2]!r}"


def test_mark_saved_with_date_sets_value(seeded_db):
    """带 date 调用 mark_saved 时应写入新值"""
    mark_saved(2, published_date="260203")

    row = fetch_row(seeded_db, 2)
    assert row[0] == 1
    assert row[2] == "260203"


def test_mark_saved_date_is_isoformat(seeded_db):
    """obsidian_saved_at 应是 ISO 格式字符串，便于后续 fromisoformat 解析"""
    mark_saved(3)

    row = fetch_row(seeded_db, 3)
    saved_at = row[1]
    # 必须可被 fromisoformat 解析（Python 3.11+ 兼容）
    parsed = datetime.fromisoformat(saved_at)
    assert parsed is not None


def test_mark_saved_idempotent_on_retry(seeded_db):
    """重复调用 mark_saved 不应丢失 published_date（重试场景）"""
    mark_saved(2, published_date="260101")
    mark_saved(2)  # 重试时不传 date
    mark_saved(2)  # 再次重试

    row = fetch_row(seeded_db, 2)
    assert row[0] == 1
    assert row[2] == "260101", "重试不应清空 published_date"
