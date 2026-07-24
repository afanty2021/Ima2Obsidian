"""mark_deleted：把已删除文章 status 改为 'deleted'，永久跳出待保存队列。

status='deleted' 自动被 get_unsaved_articles / get_stats 的 WHERE status='success'
排除（saver/reclaim/incremental 共 4 处查询一致），无需改任何 WHERE。不计入 failed，
避免反复打开已删文章（0 落盘）+ 触发上游 launchd/incremental_update 告警。
"""
import sqlite3

from ima_obsidian_saver import mark_deleted, get_unsaved_articles, get_stats


def fetch_row(db_path, article_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, published_date FROM articles WHERE id=?", (article_id,)
    ).fetchone()
    conn.close()
    return row


def test_mark_deleted_sets_status(seeded_db):
    """mark_deleted 把 status 从 'success' 改为 'deleted'"""
    assert fetch_row(seeded_db, 2)[0] == "success"  # 前置
    mark_deleted(2)
    assert fetch_row(seeded_db, 2)[0] == "deleted", f"status 应为 deleted，实际: {fetch_row(seeded_db, 2)[0]!r}"


def test_mark_deleted_idempotent(seeded_db):
    """重复调用仍是 deleted（幂等，重试安全）"""
    mark_deleted(2)
    mark_deleted(2)
    assert fetch_row(seeded_db, 2)[0] == "deleted"


def test_mark_deleted_preserves_published_date(seeded_db):
    """mark_deleted 不应破坏 published_date（与 mark_saved 的 COALESCE 保护一致）"""
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE articles SET published_date='260101' WHERE id=2")
    conn.commit()
    conn.close()

    mark_deleted(2)
    assert fetch_row(seeded_db, 2)[1] == "260101", "published_date 不应被 mark_deleted 清空"


def test_mark_deleted_excludes_from_unsaved(seeded_db):
    """标记 deleted 后，get_unsaved_articles 不再返回该篇（WHERE status='success' 自动排除）"""
    ids_before = {a["id"] for a in get_unsaved_articles(100)}
    assert 2 in ids_before  # 前置：id=2 原本在待保存队列

    mark_deleted(2)

    ids_after = {a["id"] for a in get_unsaved_articles(100)}
    assert 2 not in ids_after, "已删除文章不应再出现在待保存队列（否则每次运行都会反复打开它）"


def test_mark_deleted_not_counted_as_unsaved_in_stats(seeded_db):
    """get_stats 的 unsaved 不应包含 deleted 文章"""
    before = get_stats()
    mark_deleted(2)
    after = get_stats()
    assert after["unsaved"] == before["unsaved"] - 1, (
        f"标记 deleted 后 unsaved 应减 1（{before['unsaved']}→{after['unsaved']}）；"
        f"否则已删文章会永久卡在待保存统计里，与真实可处理数分叉"
    )
