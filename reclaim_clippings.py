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


def _safe_rename_back(dst: Path, src: Path) -> list:
    """把文件从 dst 移回 src（回滚用），失败时打印 dead-letter 提示并返回失败元组。

    dst: 当前所在位置（如 vault/AI/260102 文章.md）
    src: 要移回的原位置（如 vault/Clippings/文章.md）

    回滚失败通常意味着文件系统层面的问题（磁盘满 / 权限丢失 / 路径不存在），
    必须明确打印两个路径让运维介入；静默吞掉会导致文件位置不可知且永久漏存。

    Returns:
      [] 成功；[(dst, src, err_str), ...] 失败（供调用方汇总 dead-letter）
    """
    try:
        dst.rename(src)
        return []
    except OSError as rollback_err:
        print(
            f"  ❌ 回滚失败！文件位置不可知，需要手动恢复：\n"
            f"     当前位置: {dst}\n"
            f"     目标位置: {src}\n"
            f"     错误    : {rollback_err}"
        )
        return [(dst, src, str(rollback_err))]


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
                if not cands:
                    continue  # 本索引无该键 → 试下一索引
                available = [r for r in cands if r[0] not in claimed_article_ids]
                if len(available) > 1:
                    # 歧义：by_norm 因 normalize_stem 剥尾部 ' <数字>' 使 "X 1"/"X 2" 同键；
                    # by_sani 因 sanitize 截断使长标题共享前缀。多个未认领都不盲取首个，
                    # 以免把 clipping 记到错误文章名下 → 落 no_match。
                    break
                if not available:
                    continue  # 候选均已认领 → 试下一索引
                row = available[0]
                if idx is by_sani:
                    # by_sani 是弱兜底。sanitize 对 >240B 标题截断，使多篇长标题共享前缀键；
                    # 单候选若本身是被截断的长标题，仍可能是"幸存者"错配。故仅采信未被截断
                    # （≤240B）的候选——长标题应已由 by_norm（normalize_stem 不截断）精确命中。
                    if len((row[1] or "").encode("utf-8")) > 240:
                        row = None
                        continue
                break

            if not row:
                no_match.append(f)
                continue

            aid, title, kb, url = row
            folder = VAULT_DIR / kb if kb else None
            if not folder or not folder.is_dir():
                no_folder.append((f, kb))
                continue

            # 从正文提取发布日期：内容日期是高质量（Web Clipper 保留 *YYYY年M月D日*），
            # mtime 是低质量兜底（仅用于文件命名，不写入 DB）。
            # 区分两者：只有内容日期才允许覆盖 DB 已有值（避免 mtime 兜底误覆盖）。
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = ""
            content_date = extract_date_from_content(content)
            date_str = content_date or mtime_yymmd(f)  # 文件命名用（任意可用日期）
            target_name = f"{date_str} {sanitize_filename(title or '')}.md"
            target = folder / target_name

            if target.exists() and target.resolve() != f.resolve():
                conflict.append((f, target))
                continue

            # db_date_for_update：内容日期非空时用它（覆盖 DB 兜底/降级值），
            # 内容日期为空时传 None 让 COALESCE 保留 DB 已有值（不用 mtime 覆盖）
            matched.append((f, target, aid, date_str, content_date))
            claimed_article_ids.add(aid)
            flag = "→" if args.apply else "[DRY]"
            print(f"  {flag} {f.stem[:38]!s:40} → {kb}/{target_name[:46]}")

        # 4. 执行（仅 --apply）
        #    rename 与 UPDATE 必须保持一致。失败分类与处理：
        #      (a) UPDATE 单条失败 → 回滚该条 rename
        #      (b) commit() 失败（sqlite3.Error）→ SQLite 自动回滚 UPDATE，
        #          rename 全量回滚
        #      (c) Phase 1 期间的 BaseException（如 Ctrl+C）→ commit 还没尝试过，
        #          rename 全量回滚（事务由 close 时自动 rollback）
        #      (d) Phase 2 commit 成功后的 BaseException → 不能回滚 rename！
        #          SQLite 已提交（DB 标 saved），若把文件移回 Clippings 会变成
        #          "Clippings 孤儿 + DB 标 saved"——saver/reclaim 都只查
        #          obsidian_saved=0 → 永久漏存。修法：commit 拆出独立 try，
        #          BaseException 不在 commit 后回滚文件（保留"丑但一致"状态）。
        #      (e) 回滚 rename 也失败（磁盘满 / 权限）→ dead-letter，打印 src/dst/错误
        moved, marked = 0, 0
        rollback_failures = []  # 回滚 rename 失败的 (src, dst, err)，供 dead-letter 汇总
        renamed_pairs = []  # [(src_path, dst_path), ...] 已成功的 rename，供全量回滚
        if args.apply:
            # Phase 1: rename + UPDATE 循环
            # BaseException 在此期间触发 → commit 还没尝试 → 安全回滚
            try:
                for f, target, aid, date_str, content_date in matched:
                    try:
                        f.rename(target)
                    except OSError as e:
                        print(f"  ⚠️ 移动失败 {f.name}: {e}")
                        continue
                    renamed_pairs.append((f, target))

                    try:
                        # published_date 用 COALESCE(?, published_date)（与 mark_saved 同形式）：
                        #   - content_date 非空 → 用真实日期覆盖 DB 兜底/降级值
                        #   - content_date 为空 → 传 None，COALESCE 保留 DB 已有值
                        #     （不用 mtime 兜底误覆盖，mtime 仅用于文件命名）
                        c.execute(
                            "UPDATE articles SET obsidian_saved=1, obsidian_saved_at=?, "
                            "published_date=COALESCE(?, published_date) WHERE id=?",
                            (now_saved_at(), content_date, aid),
                        )
                    except sqlite3.Error as e:
                        # (a) UPDATE 单条失败：回滚该条 rename
                        print(f"  ⚠️ UPDATE 失败 {f.name}: {e}，回滚文件到 Clippings")
                        renamed_pairs.pop()  # 这条没保住，从已成功列表移除
                        rollback_failures.extend(_safe_rename_back(target, f))
                        continue
                    moved += 1
                    marked += 1
            except BaseException as e:
                # (c) Phase 1 期间的中断：commit 没尝试过，安全回滚
                print(f"  ❌ reclaim 中断（{type(e).__name__}: {e}），回滚已 rename 文件")
                for src, dst in renamed_pairs:
                    rollback_failures.extend(_safe_rename_back(dst, src))
                moved = 0
                marked = 0
                raise

            # Phase 2: commit（独立 try，BaseException 不在此回滚文件）
            # sqlite3.Error 时 SQLite 已自动 rollback UPDATE，对应 (b)
            # 成功后 CPython 在 committed=True 之前的字节码窗口若被 BaseException
            # 中断，文件留在 KB + DB 标 saved（一致状态），不再回滚——对应 (d)
            try:
                conn.commit()
            except sqlite3.Error as e:
                print(f"  ❌ commit 失败：{e}，开始全量回滚 {len(renamed_pairs)} 个文件到 Clippings")
                for src, dst in renamed_pairs:
                    rollback_failures.extend(_safe_rename_back(dst, src))
                moved = 0
                marked = 0
            # 注意：commit 成功后若发生 BaseException，让其在汇总前正常传播，
            # 文件保留在 KB（与已提交的 DB 一致），避免永久孤儿

    # 5. 汇总
    print("=" * 60)
    print(f"匹配并{'移动' if args.apply else '将移动'}: {len(matched)}")
    print(f"未匹配到未保存文章（保留 Clippings）: {len(no_match)}")
    print(f"匹配但 KB 无对应文件夹（保留）: {len(no_folder)}")
    print(f"目标已存在，跳过避免覆盖: {len(conflict)}")
    if args.apply:
        print(f"实际移动文件: {moved} | 标记已保存: {marked}")
        # dead-letter 汇总：commit/异常回滚时若有 rename 也失败，必须明确提示
        # （这些文件位置不可知，且下次 reclaim 因 conflict 跳过 → 永久漏存）
        if rollback_failures:
            print(f"  ⚠️  {len(rollback_failures)} 个文件回滚失败（位置不可知，需手动恢复）：")
            for dst, src, err in rollback_failures:
                print(f"     - 当前: {dst}")
                print(f"       目标: {src}")
                print(f"       错误: {err}")


if __name__ == "__main__":
    main()
