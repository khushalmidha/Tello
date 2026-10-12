"""The stock grader strips Windows profile/root variables; emulate that without network calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class WindowsEnvironmentTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific minimal-environment regression")
    def test_minimal_grader_environment_supports_sdk_and_winsock(self):
        with tempfile.TemporaryDirectory() as directory:
            module_path = str(Path(__file__).resolve().parents[1] / "harness")
            script = """
import os, sys, socket
from pathlib import Path
sys.path.insert(0, MODULE_PATH)
import harness
import anthropic
assert os.environ['USERPROFILE'] == os.environ['HOME']
assert Path.home() == Path(os.environ['HOME'])
assert socket.getaddrinfo('127.0.0.1', 8765)
anthropic.Anthropic(api_key='test-only', base_url='http://127.0.0.1:8765')
print('minimal environment works without any API request')
""".replace("MODULE_PATH", repr(module_path))
            environment = {"PATH": os.environ["PATH"], "HOME": directory, "TMP": directory, "TEMP": directory}
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
