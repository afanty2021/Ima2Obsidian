import unittest
from unittest.mock import patch, MagicMock
from ima_common import ensure_appnap_disabled


class TestEnsureAppNapDisabled(unittest.TestCase):
    @patch("ima_common.subprocess.run")
    def test_already_set_returns_true_no_write(self, mock_run):
        """defaults read 返回 1 → True，不触发 write"""
        mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
        result = ensure_appnap_disabled()
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 1)  # 只 read，不 write

    @patch("ima_common.subprocess.run")
    def test_value_zero_writes_and_returns_false(self, mock_run):
        """defaults read 返回 0（键存在但值 False）→ write → False（review plan #7）"""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="0\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch("ima_common.subprocess.run")
    def test_not_set_writes_and_returns_false(self, mock_run):
        """defaults read 非 1 → write 成功 → False"""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch("ima_common.subprocess.run")
    def test_write_failure_returns_false(self, mock_run):
        """write returncode 非 0 → False"""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="permission denied"),
        ]
        result = ensure_appnap_disabled()
        self.assertFalse(result)

    @patch("ima_common.subprocess.run")
    def test_timeout_returns_false(self, mock_run):
        """subprocess 超时 → False（不抛异常）"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="defaults", timeout=5)
        result = ensure_appnap_disabled()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
