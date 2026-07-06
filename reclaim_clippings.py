#!/usr/bin/env python3
"""
Clippings 坟场回收脚本

回收 Obsidian Web Clipper 已 clip 但 saver 未认领而滞留在 Clippings 目录的 .md 文件：
按标题匹配 DB 中 obsidian_saved=0 的文章 → 移入对应知识库文件夹 → 标记已保存。

背景：saver 旧版固定等 6s 找文件，夜间 Web Clipper 写盘慢，文件常在检查窗口外
落盘而未被认领（滞留 Clippings）。本脚本一次性回收这些历史文件，避免重新 clip。

用法:
  python3 reclaim_clippings.py              # dry-run 预览（默认，不改动）
  python3 reclaim_clippings.py --apply      # 实际移动文件 + 标记 DB
"""

import argparse
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ima_common import now_saved_at
from ima_obsidian_saver import (
    DB_FILE, VAULT_DIR, CLIPPINGS_DIR,
    sanitize_filename, extract_date_from_content,
)


def normalize_stem(s: str) -> str:
    """归一化文件名/标题用于匹配：去首尾空白 + Web Clipper 去重后缀（' 1'、' 12'）"""
    s = s.strip()
    s = re.sub(r"\s+\d+$", "", s)  # 末尾的 " <数字>" 去重后缀
    return s


def mtime_yymmd(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%y%m%d")


def _safe_rename_back(dst: Path, src: Path):
    """把文件从 dst 移回 src（回滚用），失败时打印 dead-letter 提示。

    dst: 当前所在位置（如 vault/AI/260102 文章.md）
    src: 要移回的原位置（如 vault/Clippings/文章.md）

    回滚失败通常意味着文件系统层面的问题（磁盘满 / 权限丢失 / 路径不存在），
    必须明确打印两个路径让运维介入；静默吞掉会导致文件位置不可知且永久漏存。
    """
    try:
        dst.rename(src)
    except OSError as rollback_err:
        print(
            f"  ❌ 回滚失败！文件位置不可知，需要手动恢复：\n"
            f"     当前位置: {dst}\n"
            f"     目标位置: {src}\n"
            f"     错误    : {rollback_err}"
        )


def main():
    ap = argparse.ArgumentParser(description="回收 Clippings 坟场中未认领的 clip 文件")
    ap.add_argument("--apply", action="store_true", help="实际移动文件并标记 DB（默认 dry-run）")
    args = ap.parse_args()

    if not CLIPPINGS_DIR.exists():
        print(f"❌ Clippings 目录不存在: {CLIPPINGS_DIR}")
        sys.exit(1)

    # 1. 取所有未保存文章，建标题索引
    # closing 包裹整个 DB 会话：SELECT、UPDATE、commit 全部走同一连接，
    # 任何异常（含 UPDATE 失败、commit 失败）路径都会 close，避免 fd 泄漏。
    with closing(sqlite3.connect(DB_FILE)) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, title, knowledge_base, url FROM articles "
            "WHERE status='success' AND url LIKE '%mp.weixin.qq.com%' AND obsidian_saved=0"
        )
        unsaved = c.fetchall()  # (id, title, kb, url)

        # 归一化标题 → 文章列表（可能多个文章同标题）
        by_norm: dict[str, list[tuple]] = {}
        for row in unsaved:
            aid, title, kb, url = row
            title = title or ""
            by_norm.setdefault(normalize_stem(title), []).append(row)
        # 同时建 sanitize 形式的索引兜底（Web Clipper 与 sanitize_filename 对引号等处理可能不同）
        by_sani: dict[str, list[tuple]] = {}
        for row in unsaved:
            aid, title, kb, url = row
            by_sani.setdefault(sanitize_filename(title or ""), []).append(row)

        # 2. 预建 KB 文件夹集合（只回收有对应文件夹的 KB）
        kb_folders = {p.name for p in VAULT_DIR.iterdir() if p.is_dir()} if VAULT_DIR.exists() else set()

        # 3. 扫描 Clippings 文件
        clip_files = sorted(CLIPPINGS_DIR.glob("*.md"))
        print(f"Clippings 文件: {len(clip_files)} | DB 未保存文章: {len(unsaved)} | 模式: {'实跑' if args.apply else 'DRY-RUN'}")
        print("=" * 60)

        matched, no_match, no_folder, conflict = [], [], [], []
        claimed_article_ids = set()  # 一篇文章只回收一次

        for f in clip_files:
            stem_norm = normalize_stem(f.stem)
            # 优先精确（归一化标题）匹配，其次 sanitize 匹配
            row = None
            for key, idx in ((stem_norm, by_norm), (sanitize_filename(f.stem), by_sani)):
                cands = idx.get(key)
                if cands:
                    # 取尚未被认领的那篇
                    row = next((r for r in cands if r[0] not in claimed_article_ids), None)
                    if row:
                        break

            if not row:
                no_match.append(f)
                continue

            aid, title, kb, url = row
            folder = VAULT_DIR / kb if kb else None
            if not folder or not folder.is_dir():
                no_folder.append((f, kb))
                continue

            # 从正文提取发布日期，提取不到用文件 mtime
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            date_str = extract_date_from_content(content) or mtime_yymmd(f)
            target_name = f"{date_str} {sanitize_filename(title)}.md"
            target = folder / target_name

            if target.exists() and target.resolve() != f.resolve():
                conflict.append((f, target))
                continue

            matched.append((f, target, aid, date_str))
            claimed_article_ids.add(aid)
            flag = "→" if args.apply else "[DRY]"
            print(f"  {flag} {f.stem[:38]!s:40} → {kb}/{target_name[:46]}")

        # 4. 执行（仅 --apply）
        #    rename 与 UPDATE 必须保持一致。三类失败需要回滚：
        #      (a) UPDATE 单条失败 → 回滚该条 rename
        #      (b) commit() 失败 → SQLite 自动回滚 UPDATE，但 rename 需手动全量回滚
        #      (c) 回滚 rename 也失败（磁盘满 / 权限）→ dead-letter，明确打印路径让运维介入
        #    否则文件滞留 KB 而 DB 仍 unsaved → 下次 reclaim 因 conflict 分支跳过 → 永久漏存
        moved, marked = 0, 0
        renamed_pairs = []  # [(src_path, dst_path), ...] 已成功的 rename，供 commit 失败回滚
        if args.apply:
            for f, target, aid, date_str in matched:
                try:
                    f.rename(target)
                except OSError as e:
                    print(f"  ⚠️ 移动失败 {f.name}: {e}")
                    continue
                renamed_pairs.append((f, target))

                try:
                    c.execute(
                        "UPDATE articles SET obsidian_saved=1, obsidian_saved_at=?, "
                        "published_date=COALESCE(published_date,?) WHERE id=?",
                        (now_saved_at(), date_str, aid),
                    )
                except sqlite3.Error as e:
                    # (a) UPDATE 单条失败：回滚该条 rename
                    print(f"  ⚠️ UPDATE 失败 {f.name}: {e}，回滚文件到 Clippings")
                    renamed_pairs.pop()  # 这条没保住，从已成功列表移除
                    _safe_rename_back(target, f)
                    continue
                moved += 1
                marked += 1

            # commit 阶段：失败则全量回滚
            try:
                conn.commit()
            except sqlite3.Error as e:
                print(f"  ❌ commit 失败：{e}，开始全量回滚 {len(renamed_pairs)} 个文件到 Clippings")
                for src, dst in renamed_pairs:
                    _safe_rename_back(dst, src)
                moved = 0
                marked = 0

    # 5. 汇总
    print("=" * 60)
    print(f"匹配并{'移动' if args.apply else '将移动'}: {len(matched)}")
    print(f"未匹配到未保存文章（保留 Clippings）: {len(no_match)}")
    print(f"匹配但 KB 无对应文件夹（保留）: {len(no_folder)}")
    print(f"目标已存在，跳过避免覆盖: {len(conflict)}")
    if args.apply:
        print(f"实际移动文件: {moved} | 标记已保存: {marked}")


if __name__ == "__main__":
    main()
