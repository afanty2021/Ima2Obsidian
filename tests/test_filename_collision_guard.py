"""文件名碰撞守卫测试（PR #2 审查第三轮）

覆盖 reclaim 匹配逻辑重构（by_norm 歧义 continue 落 by_sani / by_sani 按 cands 判碰撞）
及相关守卫，并订正上轮把 recall 回归钉成"正确预期"的测试。
"""
import os
import sqlite3
import sys
import time

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
        """#6: 具体断言——截断点恰为空格时结果不得以空格结尾，且长度受控。"""
        r = sanitize_filename("a" * 239 + " b" * 20)
        assert not r.endswith(" "), f"截断后不得有尾随空格，实际末尾: {r[-3:]!r}"
        assert len(r.encode("utf-8")) <= 240
        r2 = sanitize_filename("中" * 79 + " 国" * 10)
        assert not r2.endswith(" ")

    def test_short_title_unchanged(self):
        assert sanitize_filename("短标题") == "短标题"
        assert sanitize_filename("hello world") == "hello world"

    def test_none_safe(self):
        """#5: 函数本身 None-safe（不再靠调用方 or '' 掩盖）。"""
        assert sanitize_filename(None) == ""

    def test_multibyte_tail_not_corrupted(self):
        result = sanitize_filename("中" * 200)
        result.encode("utf-8")  # strict 能编码即无残缺字节


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
        """#7: 人造 252B stem 触发字节截断分支（saver 产出的 stem 上限 247B，
        生产需数千同名冲突才可达；此处直接构造长 stem 验证防御分支本身）。"""
        stem = "a" * 252  # name = 252 + ".md" = 255 字节（合法上限）
        target = tmp_path / f"{stem}.md"
        target.write_text("A", encoding="utf-8")
        src = tmp_path / "clip.md"
        src.write_text("B", encoding="utf-8")
        result = _non_conflicting_path(target, src)
        assert len(result.name.encode("utf-8")) <= 255, "序号使名字超 255B 时应截断 stem"


# ==================== find_and_rename_in_vault：防覆盖 + 容错 ====================

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
    """target_folder=None（默认 CLI）原地重命名分支也防 POSIX 覆盖。"""
    vault, clip_dir = isolated_vault
    title = "同一标题的测试文章XXXXXXXXXX"
    date = "260101"
    now = time.time()
    a_file = clip_dir / f"{date} {title}.md"
    a_file.write_text("A 原始内容", encoding="utf-8")
    os.utime(a_file, (now, now - 5))
    clip_b = clip_dir / f"{title} 1.md"
    clip_b.write_text("B 新内容", encoding="utf-8")
    os.utime(clip_b, (now, now))
    existing = {(a_file, now - 5)}

    renamed, _ = find_and_rename_in_vault(title, date, existing, target_folder=None)
    assert renamed
    assert a_file.exists() and a_file.read_text(encoding="utf-8") == "A 原始内容"
    assert (clip_dir / f"{date} {title} 2.md").exists()


def test_non_utf8_clip_does_not_crash(isolated_vault):
    """#3: errors='ignore' 避免 UnicodeDecodeError 崩溃（净改进；窄概率日期失配见 PR 评论）。"""
    vault, clip_dir = isolated_vault
    title = "含非UTF8字节的测试文章XXXXXXXXXX"
    clip = clip_dir / f"{title}.md"
    clip.write_bytes(b"# title\n\n\xff\xfe bad bytes\n")
    os.utime(clip, (clip.stat().st_atime, clip.stat().st_mtime))
    renamed, _ = find_and_rename_in_vault(title, "260101", set(), target_folder=None)
    assert renamed


# ==================== reclaim：by_norm continue / by_sani cands 判碰撞 ====================

def _setup_reclaim(tmp_path, monkeypatch, articles):
    import ima_obsidian_saver
    import reclaim_clippings
    vault = tmp_path / "Vault"
    vault.mkdir()
    clippings = tmp_path / "Clippings"
    clippings.mkdir()
    kb = "测试KB"
    (vault / kb).mkdir()
    db = str(tmp_path / "t.db")
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
    """回归：单篇精确匹配正常回收（守卫不得误伤）。"""
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, [(1, "一篇正常的文章标题")])
    (clippings / "一篇正常的文章标题.md").write_text("正文 *2026年7月22日*", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    assert _saved_counts(db) == {1: 1}


def test_reclaim_by_norm_numeric_pair_both_reclaimed(tmp_path, monkeypatch):
    """#1: 'X 1'/'X 2' 因 normalize 剥数字同键 → by_norm 歧义 continue 落到 by_sani，
    by_sani 保留 ' 1' 后缀精确消歧 → 两篇 clipping 各自回收，不丢 recall。"""
    articles = [(1, "深度学习教程第一部分 1"), (2, "深度学习教程第一部分 2")]
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, articles)
    (clippings / "深度学习教程第一部分 1.md").write_text("正文", encoding="utf-8")
    (clippings / "深度学习教程第一部分 2.md").write_text("正文", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    assert _saved_counts(db) == {1: 1, 2: 1}, "两篇应经 by_sani 精确各自回收"


def test_reclaim_by_sani_truncation_collision_skips(tmp_path, monkeypatch):
    """#4: 两篇长标题 sanitize 截到同一前缀 → by_sani cands>1 真碰撞 → break no_match（不错配）。"""
    prefix = "中" * 80  # 240 字节
    articles = [(1, prefix + "甲后缀"), (2, prefix + "乙后缀")]
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, articles)
    (clippings / f"{prefix}.md").write_text("正文", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    saved = _saved_counts(db)
    assert all(v == 0 for v in saved.values()), f"by_sani 截断碰撞应 break no_match，实际 {saved}"


def test_reclaim_by_sani_long_title_single_candidate_reclaims(tmp_path, monkeypatch):
    """#2: by_sani 是长标题唯一可行路径时（by_norm 因标题/stem 长度不一 miss），
    单候选应回收——不得因原始标题 >240B 盲跳（键唯一即无碰撞兄弟）。"""
    long_title = "中" * 82  # 246B，sanitize 截到 80 字；Web Clipper 也按字节存 80 字
    clippings, db, mod = _setup_reclaim(tmp_path, monkeypatch, [(1, long_title)])
    sanitized = sanitize_filename(long_title)  # "中"*80
    (clippings / f"{sanitized}.md").write_text("正文", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["reclaim", "--apply"])
    mod.main()
    assert _saved_counts(db) == {1: 1}, "长标题 by_sani 单候选应回收"
