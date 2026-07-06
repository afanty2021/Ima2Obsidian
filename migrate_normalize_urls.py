#!/usr/bin/env python3
"""
数据迁移脚本：规范化现有数据库中的 URL

将数据库中已有的 URL 规范化（去除微信、知乎等平台的动态参数），
确保新旧数据的一致性，避免重复保存问题。
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# 导入规范化函数与 DB_FILE（统一从 ima_common 拿，让 monkeypatch 在测试中生效）
sys.path.insert(0, Path(__file__).parent.as_posix())
from ima_ax_extractor import normalize_url
from ima_common import DB_FILE


def migrate_urls(db_file=None):
    """
    迁移数据库中的 URL。

    Args:
        db_file: 可选 DB 路径（默认 ima_common.DB_FILE，供测试注入）
    """
    db_path = db_file or DB_FILE
    print("=" * 70)
    print("数据库 URL 规范化迁移")
    print("=" * 70)
    print()

    # closing 包裹整个 DB 会话；任何异常路径都会关闭连接，避免 fd 泄漏。
    with closing(sqlite3.connect(db_path)) as conn:
        c = conn.cursor()

        # 1. 获取所有记录
        print("1. 读取现有数据...")
        c.execute("SELECT id, url, title, knowledge_base FROM articles")
        records = c.fetchall()
        print(f"   共 {len(records)} 条记录")
        print()

        # 2. 分析需要迁移的记录
        print("2. 分析需要迁移的记录...")
        migrations = []
        for record_id, url, title, kb in records:
            normalized = normalize_url(url)
            if normalized != url:
                migrations.append({
                    'id': record_id,
                    'old_url': url,
                    'new_url': normalized,
                    'title': title,
                    'kb': kb
                })

        print(f"   需要迁移: {len(migrations)} 条记录")
        print()

        if not migrations:
            print("✅ 无需迁移，所有 URL 已规范化")
            return

        # 3. 显示迁移预览
        print("3. 迁移预览（前10条）:")
        print("-" * 70)
        for i, m in enumerate(migrations[:10], 1):
            print(f"{i}. [{m['kb']}] {m['title'][:40] if m['title'] else ''}...")
            print(f"   旧: {m['old_url'][:60]}...")
            print(f"   新: {m['new_url'][:60]}...")
            print()

        if len(migrations) > 10:
            print(f"... 还有 {len(migrations) - 10} 条记录")
            print()

        # 4. 执行迁移
        #    去重时必须先把 obsidian_saved/obsidian_saved_at/published_date 合并到
        #    保留行，再 DELETE 重复行。否则可能把"已保存"行删掉、留下未规范行的
        #    "未保存"状态 → vault 里 .md 已存在但 DB 标 unsaved → 永久漏存。
        #    整个 for 循环在 try 内：'database is locked' 等异常命中 SELECT/UPDATE/DELETE
        #    任一语句时，closing 自动回滚整批 UPDATE/DELETE，避免半完成状态。
        print("4. 开始迁移...")
        success_count = 0
        merged_count = 0  # 合并去重的行数（不是错误，单独维度）

        try:
            for m in migrations:
                try:
                    c.execute("""
                        UPDATE articles
                        SET url = ?
                        WHERE id = ?
                    """, (m['new_url'], m['id']))
                    success_count += 1
                except sqlite3.IntegrityError:
                    # 规范化后 URL 与已有行冲突：先把当前行的元数据合并到保留行
                    # （保留行 = 已有 url=m['new_url'] 的行），再 DELETE 当前行
                    c.execute(
                        "SELECT obsidian_saved, obsidian_saved_at, published_date "
                        "FROM articles WHERE id = ?",
                        (m['id'],),
                    )
                    del_row = c.fetchone()
                    if del_row:
                        del_saved, del_saved_at, del_pub = del_row
                        # 合并到保留行：
                        #   obsidian_saved: 1 wins（任一为 1 则保留行为 1）
                        #   obsidian_saved_at: 保留行优先（COALESCE(keeper, victim)）
                        #   published_date: 保留行优先
                        c.execute(
                            "UPDATE articles SET "
                            "obsidian_saved = MAX(COALESCE(?, 0), COALESCE(obsidian_saved, 0)), "
                            "obsidian_saved_at = COALESCE(obsidian_saved_at, ?), "
                            "published_date = COALESCE(published_date, ?) "
                            "WHERE url = ? AND id != ?",
                            (del_saved, del_saved_at, del_pub, m['new_url'], m['id']),
                        )
                    c.execute("DELETE FROM articles WHERE id = ?", (m['id'],))
                    merged_count += 1
                    print(f"   ℹ️  ID {m['id']} 规范化后与保留行重复，已合并元数据并删除")
                # 注意：不再有 except Exception — 'database is locked' 等其他 sqlite3.Error
                # 必须穿透到外层 except sqlite3.Error，触发 rollback 让本次迁移原子失败，
                # 避免半完成状态。其他真异常（KeyError 等）也照常穿透、由调用栈处理。
        except sqlite3.Error as e:
            # 锁冲突 / 磁盘 I/O 等：closing 会自动 rollback，本次迁移失败但不会留半完成状态
            print(f"   ❌ 迁移中断（{e}）；已成功的 UPDATE/DELETE 已回滚，请重试")
            conn.rollback()
            raise

        conn.commit()
        print(f"   ✅ 成功: {success_count} 条")
        if merged_count > 0:
            print(f"   🔀 合并去重: {merged_count} 条（不算失败）")
        print()

        # 5. 验证去重效果
        print("5. 验证去重效果...")
        c.execute("SELECT COUNT(*) FROM articles")
        total_before = c.fetchone()[0]

        c.execute("SELECT COUNT(DISTINCT url) FROM articles")
        unique_after = c.fetchone()[0]

        duplicates_removed = total_before - unique_after

        print(f"   总记录数: {total_before}")
        print(f"   唯一URL数: {unique_after}")

        if duplicates_removed > 0:
            print(f"   ⚠️  发现 {duplicates_removed} 个重复URL，需要清理")
            c.execute("""
                SELECT url, COUNT(*) as count
                FROM articles
                GROUP BY url
                HAVING count > 1
                ORDER BY count DESC
                LIMIT 5
            """)
            duplicates = c.fetchall()
            print(f"   重复示例（前5个）:")
            for url, count in duplicates:
                print(f"   - {url[:50]}... (出现 {count} 次)")
        else:
            print(f"   ✅ 无重复URL")

    print()
    print("=" * 70)
    print("迁移完成！")
    print("=" * 70)


if __name__ == "__main__":
    try:
        migrate_urls()
    except KeyboardInterrupt:
        print("\n⚠️  迁移被用户中断")
    except Exception as e:
        print(f"❌ 迁移失败: {e}")
        sys.exit(1)
