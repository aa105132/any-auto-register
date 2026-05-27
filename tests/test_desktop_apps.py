from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.desktop_apps import _run_command


class DesktopAppsTests(unittest.TestCase):
    def test_run_command_captures_bytes_and_decodes_with_replacement(self):
        completed = SimpleNamespace(returncode=0, stdout=b'"cmd.exe"\r\n\xd4', stderr=b"")
        with patch("core.desktop_apps.subprocess.run", return_value=completed) as run:
            ok, output = _run_command(["tasklist", "/FO", "CSV", "/NH"])

        self.assertTrue(ok)
        self.assertIsInstance(output, str)
        self.assertIn('"cmd.exe"', output)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(kwargs.get("stderr"), subprocess.PIPE)
        self.assertNotEqual(kwargs.get("text"), True)


if __name__ == "__main__":
    unittest.main()
