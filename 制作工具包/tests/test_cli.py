"""验证用户实际命令入口、续跑与失败退出；不调用真实模型。"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from voicekit import __main__ as cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中文项目"
        self.root.mkdir()
        package = Path(__file__).resolve().parents[1]
        shutil.copytree(package / "voicekit", self.root / "voicekit", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(package / "examples", self.root / "examples")
        shutil.copyfile(package / "config.example.json", self.root / "config.example.json")

    def run_cli(self, *args, expected=0):
        process = subprocess.run([sys.executable, "-B", "-m", "voicekit", *args],
                                 cwd=self.root, capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return json.loads(process.stdout if expected == 0 else process.stderr)

    def test_demo_runs_and_resumes_but_cannot_export_as_real(self):
        first = self.run_cli("demo")
        self.assertEqual(first["generation"]["completed"], 7)
        self.assertEqual(first["simulated_evidence"]["completed"], 7)
        self.assertEqual(first["review"]["formal_adoptions"], 0)
        pack = json.loads(Path(first["export"]).read_text(encoding="utf-8"))
        self.assertEqual(pack["delivery"]["status"], "DEMO_NOT_VOICE")
        self.assertEqual(len([n for n in pack["nodes"] if n["kind"] == "line"]), 7)
        second = self.run_cli("demo")
        self.assertEqual(second["generation"]["skipped"], 7)
        self.assertEqual(second["simulated_evidence"]["skipped"], 7)
        self.assertIn("error", self.run_cli("export", expected=2))
        self.assertIn("error", self.run_cli("accept", "--line", "guard", "--reason", "演示不能正式采纳", expected=2))

    def test_changed_source_requires_new_prepare(self):
        self.run_cli("demo")
        source = self.root / "examples" / "story.jsonl"
        source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        result = self.run_cli("review", expected=2)
        self.assertIn("prepare", result["error"])

    def test_nested_demo_failure_stops_before_export(self):
        for failing_stage in ("generate", "transcribe", "review"):
            with self.subTest(stage=failing_stage), contextlib.ExitStack() as stack:
                output, error = io.StringIO(), io.StringIO()
                stack.enter_context(contextlib.redirect_stdout(output))
                stack.enter_context(contextlib.redirect_stderr(error))
                stack.enter_context(patch.object(cli.core, "load_project", return_value={"tts": {"backend": "demo"}}))
                stack.enter_context(patch.object(cli.core, "prepare", return_value={}))
                failure = {"errors": [{"error": "模拟阶段错误"}], "halted": True}
                success = {"errors": [], "halted": False}
                for name in ("generate", "transcribe"):
                    stack.enter_context(patch.object(cli.backends, name, return_value=failure if name == failing_stage else success))
                stack.enter_context(patch.object(cli.core, "review", return_value=failure if failing_stage == "review" else success))
                exported = stack.enter_context(patch.object(cli.core, "export_pack"))
                self.assertEqual(cli.main(["demo"]), 2)
                exported.assert_not_called()
                self.assertIn("模拟阶段错误", json.loads(error.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
