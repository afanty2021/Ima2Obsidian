import unittest
import tempfile
from pathlib import Path
from reclaim_clippings import _compute_batch_corrupt_skipped


class TestComputeBatchCorruptSkipped(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.d = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_file(self, name, content):
        f = self.d / name
        f.write_text(content, encoding="utf-8")
        return f

    def test_batch_corrupt_same_md5_different_stems(self):
        """8/2 模式：同内容 + 不同文件名 → 整组跳过"""
        f1 = self._make_file("文章A.md", "same content")
        f2 = self._make_file("文章B.md", "same content")
        f3 = self._make_file("文章C.md", "same content")
        skipped = _compute_batch_corrupt_skipped([f1, f2, f3])
        self.assertEqual(skipped, {f1, f2, f3})

    def test_legitimate_reclip_same_md5_normalized_same_stem(self):
        """合法重 clip：3 文件同内容 + normalize_stem 后同名（'文章'/'文章 1'/'文章 2'）→ 不跳过
        阈值 ≥3 让组进入 stems 比较，验证 normalize_stem 剥序号生效（PR#11 #4 修正）"""
        f1 = self._make_file("文章.md", "same content")
        f2 = self._make_file("文章 1.md", "same content")
        f3 = self._make_file("文章 2.md", "same content")
        skipped = _compute_batch_corrupt_skipped([f1, f2, f3])
        self.assertEqual(skipped, set())

    def test_unique_md5_not_skipped(self):
        """md5 唯一 → 不跳过"""
        f1 = self._make_file("文章A.md", "content A")
        f2 = self._make_file("文章B.md", "content B")
        skipped = _compute_batch_corrupt_skipped([f1, f2])
        self.assertEqual(skipped, set())

    def test_single_file_not_skipped(self):
        """单文件（组大小=1）→ 不跳过"""
        f1 = self._make_file("文章.md", "content")
        skipped = _compute_batch_corrupt_skipped([f1])
        self.assertEqual(skipped, set())

    def test_two_different_articles_same_content_not_flagged(self):
        """2 篇不同文章同内容（如错误页）+ 不同名 → 不判为批量错乱（阈值 ≥3）"""
        f1 = self._make_file("文章A.md", "same error page content")
        f2 = self._make_file("文章B.md", "same error page content")
        skipped = _compute_batch_corrupt_skipped([f1, f2])
        self.assertEqual(skipped, set())


if __name__ == "__main__":
    unittest.main()
