#!/usr/bin/env python3
"""
数据迁移脚本：规范化现有数据库中的 URL

将数据库中已有的 URL 规范化（去除微信、知乎等平台的动态参数），
确保新旧数据的一致性，避免重复保存问题。
"""

import sqlite3
import sys
from pathlib import Path

# 导入规范化函数
sys.path.insert(0, Path(__file__).parent.as_posix())
from ima_ax_extractor import normalize_url

DB_FILE = Path(__file__).parent / "ima_articles.db"

def migrate_urls():
    """迁移数据库中的 URL"""
    print("=" * 70)
    print("数据库 URL 规范化迁移")
    print("=" * 70)
    print()

    conn = sqlite3.connect(DB_FILE)
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
        conn.close()
        return

    # 3. 显示迁移预览
    print("3. 迁移预览（前10条）:")
    print("-" * 70)
    for i, m in enumerate(migrations[:10], 1):
        print(f"{i}. [{m['kb']}] {m['title'][:40]}...")
        print(f"   旧: {m['old_url'][:60]}...")
        print(f"   新: {m['new_url'][:60]}...")
        print()

    if len(migrations) > 10:
        print(f"... 还有 {len(migrations) - 10} 条记录")
        print()

    # 4. 确认迁移
    print("4. 开始迁移...")
    success_count = 0
    error_count = 0

    for m in migrations:
        try:
            c.execute("""
                UPDATE articles
                SET url = ?
                WHERE id = ?
            """, (m['new_url'], m['id']))
            success_count += 1
        except Exception as e:
            print(f"   ❌ 迁移失败 ID {m['id']}: {e}")
            error_count += 1

    conn.commit()
    print(f"   ✅ 成功: {success_count} 条")
    if error_count > 0:
        print(f"   ❌ 失败: {error_count} 条")
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

        # 显示重复的记录
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

    conn.close()
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