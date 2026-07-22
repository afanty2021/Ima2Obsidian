"""文件名碰撞守卫测试（PR #2 审查第二轮）

覆盖审查 33244ff 增量发现并修复的 7 项：
- #1 find_and_rename_in_vault 的 target_folder=None 原地重命名分支也防 POSIX 覆盖
- #2 reclaim by_norm 数字后缀剥离歧义 / by_sani 字节截断碰撞歧义
- #3 read_text 非 UTF-8 字节不触发未捕获 UnicodeDecodeError
- #4 by_sani 单候选若为被截断长标题则跳过（截断碰撞幸存者错配）
- #5 本测试文件本身——把守卫持久化，回退即红
- #6 _non_conflicting_path 序号使名字超 255 字节时按字节截断 stem
- #7 _non_conflicting_path 与 reclaim 策略相反（保留 vs 跳过）已在 docstring 订正
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from ima_obsidian_saver import (
    sanitize_filename,
    _non_conflicting_path,
    find_and_rename_in_vault,
)


# ==================== sanitize_filename：字节截断 / 尾随空格 / None ====================

class TestSanitizeFilenameBytes:
    def test_cjk_long_title_under_255_bytes(self):
        title = "中" * 200  # 600 字节原始
        full = f"260722 {sanitize_filename(title)}.md"
        assert len(full.encode("utf-8")) <= 255

    def test_no_trailing_space_after_byte_truncation(self):
        # 截断点恰为空格时不应留尾随空格（.md 前不能有空格）
        assert sanitize_filename("a" * 239 + " b" * 20) == sanitize_filename("a" * 239 + " b" * 20).rstrip()
        assert sanitize_filename("中" * 79 + " 国" * 10) == sanitize_filename("中" * 79 + " 国" * 10).rstrip()

    def test_short_title_unchanged(self):
        assert sanitize_filename("短标题") == "短标题"
        assert sanitize_filename("hello world") == "hello world"

    def test_none_safe(self):
        # None 经 (None or "") 不应触发 re.sub 对 None 的 TypeError
        assert sanitize_filename(None or "") == ""

    def test_multibyte_tail_not_corrupted(self):
        # 截断不得落在多字节字符中间留下残缺字节
        result = sanitize_filename("中" * 200)
        result.encode("utf-8")  # strict 模式能编码即说明无残缺


# ==================== _non_conflicting_path：序号守卫 + 字节上限 ====================

class TestNonConflictingPath:
    def test_appends_suffix_when_target_exists(self, tmp_path):
        target = tmp_path / "260722 标题.md"
        target.write_text("A", encoding="utf-8")
        src = tmp_path / "clip.md"
        src.write_text("B", encoding="utf-8")
        assert _non_conflicting_path(target, src).name == "260722 标题 2.md"

    def test_increments_suffix(self, tmp_path):
        target = tmp_path / "260722 标题.md"
        target.write_text("A", encoding="utf-8")
        (tmp_path / "260722 标题 2.md").write_text("C", encoding="utf-8")
        src = tmp_path / "clip.md"
        src.write_text("B", encoding="utf-8")
        assert _non_conflicting_path(target, src).name == "260722 标题 3.md"

    def test_unchanged_when_target_absent(self, tmp_path):
        src = tmp_path / "clip.md"
        src.write_text("B", encoding="utf-8")
        assert _non_conflicting_path(tmp_path / "new.md", src).name == "new.md"

    def test_unchanged_when_target_is_self(self, tmp_path):
        target = tmp_path / "260722 标题.md"
        target.write_text("A", encoding="utf-8")
        assert _non_conflicting_path(target, target).name == "260722 标题.md"

    def test_truncates_when_suffix_exceeds_byte_limit(self, tmp_path):
        """#6: target 名恰 255 字节（上限），序号一加即超 255 → 按 bytes 截断 stem。"""
        stem = "a" * 252  # name = 252 + ".md" = 255 字节（合法上限）
        target = tmp_path / f"{stem}.md"
        target.write_text("A", encoding="utf-8")  # 255 字节名，可创建
        src = tmp_path / "clip.md"
        src.write_text("B", encoding="utf-8")
        result = _non_conflicting_path(target, src)
        assert len(result.name.encode("utf-8")) <= 255, "序号使名字超 255B 时应截断 stem，不应返回非法路径"


# ==================== find_and_rename_in_vault：#1 防覆盖 + #3 容错 ====================

@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    vault = tmp_path / "Vault"
    vault.mkdir()
    clip_dir = vault / "Clippings"
    clip_dir.mkdir()
    monkeypatch.setattr("ima_obsidian_saver.VAULT_DIR", vault)
    monkeypatch.setattr("ima_obsidian_saver.CLIPPINGS_DIR", clip_dir)
    return vault, clip_dir


def test_target_folder_none_branch_no_overwrite(isolated_vault):
    """#1: target_folder=None（默认/文档化 CLI 路径）原地重命名分支也必须防覆盖。
    两篇同标题文章顺序处理时，第二篇不得 POSIX 覆盖第一篇已落盘的 .md。"""
    vault, clip_dir = isolated_vault
    title = "同一标题的测试文章XXXXXXXXXX"  # len > 10 过 substring gate
    date = "260101"
    now = time.time()

    # A 已落盘（模拟第一篇已处理的结果）
    a_file = clip_dir / f"{date} {title}.md"
    a_file.write_text("A 原始内容", encoding="utf-8")
    os.utime(a_file, (now, now - 5))  # A 稍旧
    # B 的 clip（Web Clipper 去重名），mtime 更新 → 成为 candidates[0]
    clip_b = clip_dir / f"{title} 1.md"
    clip_b.write_text("B 新内容", encoding="utf-8")
    os.utime(clip_b, (now, now))
    existing = {(a_file, now - 5)}  # A 视作已存在（非新文件）

    renamed, _ = find_and_rename_in_vault(title, date, existing, target_folder=None)
    assert renamed
    # A 必须原样保留（未被覆盖）
    assert a_file.exists(), "第一篇 A 不应被覆盖"
    assert a_file.read_text(encoding="utf-8") == "A 原始内容", "A 内容被覆盖 → 数据丢失"
    # B 应被重命名到序号路径，而非顶替 A
    assert (clip_dir / f"{date} {title} 2.md").exists(), "第二篇应得序号后缀"


def test_non_utf8_clip_does_not_crash(isolated_vault):
    """#3: 含非 UTF-8 字节的 clip，read_text 不应抛未被 except OSError 捕获的
    UnicodeDecodeError（ValueError 子类）使整篇保存崩溃。"""
    vault, clip_dir = isolated_vault
    title = "含非UTF8字节的测试文章XXXXXXXXXX"
    clip = clip_dir / f"{title}.md"
    clip.write_bytes(b"# title\n\n\xff\xfe bad bytes\n")  # 混杂非 UTF-8 字节
    os.utime(clip, (clip.stat().st_atime, clip.stat().st_mtime))

    # 不应抛 UnicodeDecodeError
    renamed, _ = find_and_rename_in_vault(title, "260101", set(), target_folder=None)
    assert renamed  # 正常处理完成


# ==================== reclaim：#2/#4 歧义守卫 + 回归 ====================

def _setup_reclaim(tmp_path, monkeypatch, articles):
    """构造隔离 DB + Vault + Clippings，返回 (clippings, db_path, reclaim_module)。
    articles: [(aid, title), ...]，均为 mp.weixin 未保存文章。"""
    import ima_obsidian_saver
    import reclaim_clippings
    vault = tmp_path / "Vault"
    vault.mkdir()
    clippings = tmp_path / "Clippings"
    clippings.mkdir()
    kb = "测试KB"
    (vault / kb).mkdir()
    db = str(tmp_path / "t.db")
    # 三处 DB_FILE 都指向 tmp（reclaim/saver 在 import 时已绑定值）
    monkeypatch.setattr("ima_common.DB_FILE", db)
    monkeypatch.setattr(ima_obsidian_saver, "DB_FILE", db)
    monkeypatch.setattr(reclaim_clippings, "DB_FILE", db)
    monkeypatch.setattr(reclaim_clippings, "VAULT_DIR", vault)
    monkeypatch.setattr(reclaim_clippings, "CLIPPINGS_DIR", clippings)

    from ima_common import init_database
    init_database()
    conn = sqlite3.connect(db)
    for aid, t in articles:
        conn.execute(
            "INSERT INTO articles(id,url,title,knowledge_base,status,obsidian_saved)"
            " VALUES(?,?,?,?,?,0)",
            (aid, f"https://mp.weixin.qq.com/s/{aid}", t, kb, "success"),
        )
    conn.commit()
    conn.close()
    return clippings, db, reclaim_clippings


def _saved_counts(db):
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id, obsidian_saved FROM articles").fetchall()
    conn.close()
    return dict(rows)


def test_reclaim_single_article_matched(tmp_path, monkeypatch):
    """回归：单篇文章 + 精确 clipping 应正常认领（守卫不得误伤）。"""
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, [(1, "一篇正常的文章标题")])
    (clippings / "一篇正常的文章标题.md").write_text("正文 *2026年7月22日*", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    assert _saved_counts(db) == {1: 1}


def test_reclaim_by_norm_numeric_suffix_collision_skips(tmp_path, monkeypatch):
    """#2: normalize_stem 剥尾部 ' <数字>'，'X 1'/'X 2' 归一化同键 → 歧义跳过，不盲取首个。"""
    articles = [(1, "深度学习教程第一部分 1"), (2, "深度学习教程第一部分 2")]
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, articles)
    (clippings / "深度学习教程第一部分 1.md").write_text("正文", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    saved = _saved_counts(db)
    assert all(v == 0 for v in saved.values()), f"by_norm 数字后缀歧义应跳过，实际 {saved}"


def test_reclaim_by_sani_truncation_collision_skips(tmp_path, monkeypatch):
    """#2: 两篇 CJK 长标题 sanitize 截到同一 80 字前缀 → by_sani 多候选歧义跳过。"""
    prefix = "中" * 80  # 240 字节
    articles = [(1, prefix + "甲后缀"), (2, prefix + "乙后缀")]
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, articles)
    (clippings / f"{prefix}.md").write_text("正文", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    saved = _saved_counts(db)
    assert all(v == 0 for v in saved.values()), f"by_sani 截断碰撞歧义应跳过，实际 {saved}"


def test_reclaim_by_sani_long_title_single_candidate_skips(tmp_path, monkeypatch):
    """#4: A 由自身完整 clipping 经 by_norm 精确认领；截断前缀孤儿再处理时，
    by_sani 单候选 B（长标题，截断同前缀）应被跳过，不误配给 B。"""
    prefix = "中" * 80  # 240 字节；prefix+"甲" = 243 字节会被 sanitize 截断
    articles = [(1, prefix + "甲"), (2, prefix + "乙")]
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, articles)
    (clippings / f"{prefix}甲.md").write_text("正文", encoding="utf-8")  # A 的完整 clipping
    (clippings / f"{prefix}.md").write_text("孤儿", encoding="utf-8")   # 截断前缀孤儿
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    saved = _saved_counts(db)
    assert saved.get(1) == 1, "A 应由自身完整 clipping 经 by_norm 精确认领"
    assert saved.get(2) == 0, "B 不应被截断前缀孤儿误认领（长标题 by_sani 跳过）"
