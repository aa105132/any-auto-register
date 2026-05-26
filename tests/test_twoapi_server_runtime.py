from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.twoapi.server_runtime import TwoAPIServerRuntime


class TwoAPIServerRuntimeTests(unittest.TestCase):
    def test_ensure_running_reuses_existing_port_without_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TwoAPIServerRuntime(root=Path(tmp), data_dir=Path(tmp) / "output", wait_interval=0)
            runtime._is_port_open = Mock(return_value=True)
            with patch("services.twoapi.server_runtime.subprocess.Popen") as popen:
                result = runtime.ensure_running(timeout_seconds=0.1)
            self.assertTrue(result["running"])
            self.assertFalse(result["started"])
            popen.assert_not_called()

    def test_ensure_running_starts_script_with_file_logs_and_utf8_environment(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts" / "run_twoapi_server.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('server')", encoding="utf-8")
            runtime = TwoAPIServerRuntime(root=root, data_dir=root / "output", wait_interval=0)
            runtime._is_port_open = Mock(side_effect=[False, True])
            process = Mock(pid=4321)
            process.poll.return_value = None
            with patch("services.twoapi.server_runtime.subprocess.Popen", return_value=process) as popen:
                result = runtime.ensure_running(timeout_seconds=0.1)

            self.assertTrue(result["running"])
            self.assertTrue(result["started"])
            self.assertEqual(result["pid"], 4321)
            args, kwargs = popen.call_args
            self.assertEqual(Path(args[0][1]), script)
            self.assertEqual(Path(kwargs["cwd"]), root)
            self.assertEqual(kwargs["env"]["TWOAPI_CHILD_SERVER"], "1")
            self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertNotEqual(kwargs.get("text"), True)
            self.assertNotEqual(kwargs.get("stdout"), subprocess.PIPE)
            self.assertTrue((root / "output" / "twoapi_server.log").exists())
            self.assertTrue((root / "output" / "twoapi_server.err.log").exists())


if __name__ == "__main__":
    unittest.main()
