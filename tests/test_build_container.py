import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BuildContainerTest(unittest.TestCase):
    def test_wrapper_mounts_private_data_read_only_without_network_or_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "private"
            source.mkdir()
            (source / "affiliations.json").write_text("{}")
            log = root / "calls.jsonl"
            docker = root / "docker"
            docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
with open(os.environ['FAKE_DOCKER_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'build': print('sha256:fixture')
''')
            docker.chmod(0o755)
            environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"],
                               GITHUB_OTEL_DATA=str(source), GRAFANA_OTEL_OUTPUT=str(root / "output"),
                               FAKE_DOCKER_LOG=str(log), GH_READ_TOKEN="do-not-forward")
            command = ["bash", str(ROOT / "scripts/build.sh"), "--start", "2024-01-01", "--end", "2024-01-02"]
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            build, run = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(build[-1], str(ROOT))
            self.assertIn(f"type=bind,source={source},target=/source,readonly", run)
            self.assertEqual(run[run.index("--network") + 1], "none")
            self.assertIn("--read-only", run)
            self.assertEqual(run[run.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
            self.assertEqual(run[-4:], ["--start", "2024-01-01", "--end", "2024-01-02"])
            self.assertNotIn("GH_READ_TOKEN", run)
            self.assertNotIn("do-not-forward", log.read_text())
            log.unlink()
            environment["GRAFANA_OTEL_OUTPUT"] = str(source / "nested")
            result = subprocess.run(command, env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((source / "nested").exists())
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
