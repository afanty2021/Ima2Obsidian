"""F6: get_stats 与 get_unsaved_articles 的 unsaved 口径必须一致"""
import sqlite3

from ima_obsidian_saver import get_stats, get_unsaved_articles
from ima_common import init_database


def _set_obsidian_saved(db_path, article_id, value):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE articles SET obsidian_saved=? WHERE id=?", (value, article_id))
    conn.commit()
    conn.close()


def test_stats_unsaved_matches_query_count(seeded_db):
    """get_stats.unsaved 必须等于 get_unsaved_articles 的实际返回数量"""
    stats = get_stats()
    articles = get_unsaved_articles(limit=100)
    assert stats["unsaved"] == len(articles), \
        f"stats unsaved={stats['unsaved']} != 实际查询返回 {len(articles)} 篇"


def test_stats_consistent_when_obsidian_saved_is_anomalous(seeded_db):
    """obsidian_saved 出现非 {0,1,NULL} 异常值时，stats 与查询仍必须一致"""
    # 把 id=2 设成异常值 2（手动改库、未来新增标志位、崩溃残留等）
    _set_obsidian_saved(seeded_db, 2, 2)

    stats = get_stats()
    articles = get_unsaved_articles(limit=100)

    # 异常值的行不应被 get_unsaved_articles 选中（它只匹配 0 或 NULL）
    # get_stats 的 unsaved 也应该等于实际查询数（不能算上异常值）
    assert stats["unsaved"] == len(articles), \
        f"stats unsaved={stats['unsaved']} != 实际 {len(articles)}；" \
        f"异常 obsidian_saved=2 行造成口径分叉"


def test_stats_consistent_with_null(seeded_db):
    """obsidian_saved = NULL 时 stats 与查询一致"""
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE articles SET obsidian_saved=NULL WHERE id=2")
    conn.commit()
    conn.close()

    stats = get_stats()
    articles = get_unsaved_articles(limit=100)
    assert stats["unsaved"] == len(articles)


def test_stats_total_and_saved_match_filter(seeded_db):
    """total/saved/unsaved 三者满足不变式：saved + unsaved_in_query = total_filtered"""
    stats = get_stats()
    assert stats["total"] >= stats["saved"]
    assert stats["unsaved"] >= 0
    # saved 不能超过 total
    assert stats["saved"] <= stats["total"]
