"""后端契约测试：临时目录、本地模拟 HTTP 和伪模型，不调用真实服务/模型。"""

from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from voicekit import backends, core


@contextmanager
def mock_api(response=None, status=200):
    requests = []
    body = backends._demo_wave() if response is None else response

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({"path": self.path,
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav" if status == 200 else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tts", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference = self.root / "reference.wav"
        self.reference.write_bytes(backends._demo_wave())
        self.target = "请在三号门等我，不要离开。"
        story = [
            {"id": "line1", "kind": "line", "speaker": "守卫", "display_text": self.target,
             "reading_text": self.target, "voice": "guard", "path_id": "", "next_id": "end",
             "source_ref": "fictional-test/1", "reading_change_reason": "无"},
            {"id": "end", "kind": "end", "speaker": "", "display_text": "结束", "path_id": ""},
        ]
        (self.root / "story.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in story), encoding="utf-8")
        self.config = {
            "schema_version": 1, "project_id": "backend-test", "title": "测试用虚构剧情",
            "source": "story.jsonl", "work_dir": "work", "output_dir": "output",
            "entry_id": "line1", "routes_confirmed": True,
            "voices": {"guard": {"reference_audio": "reference.wav", "prompt_text": "参考声音的文字。", "prompt_lang": "zh"}},
            "tts": {"backend": "gpt_sovits_api", "engine_id": "test-only-mock-weights-v1",
                    "parameters": {"seed": 42}}, "asr": {},
        }

    def prepare(self, config=None):
        config = copy.deepcopy(config or self.config)
        filename = self.root / "config.json"
        filename.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        project = core.load_project(filename)
        return project, core.prepare(project)

    def generated_project(self, asr=None):
        with mock_api() as (url, _):
            self.config["tts"]["api_url"] = url
            self.config["asr"] = asr or {}
            project, manifest = self.prepare()
            report = backends.generate(project, manifest)
            self.assertEqual(report["completed"], 1, report)
        return project, manifest

    def test_api_request_reference_binding_and_verified_resume(self):
        with mock_api() as (url, requests):
            self.config["tts"]["api_url"] = url
            self.config["voices"]["guard"]["server_ref_audio_path"] = "/server/voice/reference.wav"
            project, manifest = self.prepare()
            report = backends.generate(project, manifest)
            self.assertEqual(report["completed"], 1, report)
            payload = requests[0]["body"]
            self.assertEqual(payload["text"], self.target)
            self.assertEqual(payload["prompt_text"], "参考声音的文字。")
            self.assertEqual(payload["ref_audio_path"], "/server/voice/reference.wav")
            self.assertIs(payload["streaming_mode"], False)
            self.assertEqual(payload["media_type"], "wav")
            self.assertEqual(requests[0]["path"], "/tts")
            candidate = core.get_candidate(project, "line1")
            self.assertFalse(candidate["demo"])
            self.assertEqual(candidate["details"]["reference_sha256"], core.sha256(self.reference))
            self.assertEqual(backends.generate(project, manifest)["skipped"], 1)
            self.assertEqual(len(requests), 1)

    def test_changed_reference_rejected_before_api(self):
        with mock_api() as (url, requests):
            self.config["tts"]["api_url"] = url
            project, manifest = self.prepare()
            self.reference.write_bytes(b"changed-reference")
            report = backends.generate(project, manifest)
            self.assertTrue(report["errors"])
            self.assertEqual(len(requests), 0)

    def test_response_failure_is_recorded_without_automatic_retry(self):
        with mock_api(response=b'{"error":"test failure"}', status=400) as (url, requests):
            self.config["tts"]["api_url"] = url
            project, manifest = self.prepare()
            first = backends.generate(project, manifest)
            second = backends.generate(project, manifest)
            self.assertTrue(first["errors"])
            self.assertTrue(second["errors"])
            self.assertEqual(len(requests), 1)
            folder = core.candidate_path(project, "line1").parent
            self.assertEqual(core.read_json(folder / "generation_attempt.json")["status"], "failed_or_unconfirmed")
            self.assertFalse(core.candidate_path(project, "line1").exists())

    def test_invalid_or_truncated_wav_never_becomes_candidate(self):
        for body in (b'{"not":"audio"}', backends._demo_wave()[:-8]):
            with self.subTest(body_size=len(body)), mock_api(response=body) as (url, _):
                self.config["tts"]["api_url"] = url
                self.config["tts"]["engine_id"] += "-next"
                project, manifest = self.prepare()
                self.assertTrue(backends.generate(project, manifest)["errors"])
                self.assertFalse(core.candidate_path(project, "line1").exists())

    def test_parameters_cannot_override_bound_text_or_stream_mode(self):
        for forbidden in ("text", "ref_audio_path", "streaming_mode", "media_type"):
            with self.subTest(parameter=forbidden), mock_api() as (url, requests):
                config = copy.deepcopy(self.config)
                config["tts"]["api_url"] = url
                config["tts"]["parameters"][forbidden] = "unexpected"
                project, manifest = self.prepare(config)
                self.assertTrue(backends.generate(project, manifest)["errors"])
                self.assertEqual(requests, [])

    def test_candidate_tamper_blocks_resume(self):
        project, manifest = self.generated_project()
        core.candidate_path(project, "line1").write_bytes(b"tampered")
        self.assertTrue(backends.generate(project, manifest)["errors"])

    def test_demo_is_explicit_simulation_and_real_asr_refuses_it(self):
        config = copy.deepcopy(self.config)
        config["tts"] = {"backend": "demo", "engine_id": "demo-test-v1"}
        config["voices"] = {"guard": {"label": "演示"}}
        project, manifest = self.prepare(config)
        self.assertEqual(backends.generate(project, manifest, demo=True)["completed"], 1)
        candidate = core.get_candidate(project, "line1", demo=True)
        self.assertTrue(candidate["demo"])
        self.assertTrue(candidate["details"]["synthetic"])
        self.assertIn("提示音", candidate["details"]["simulation"])
        self.assertFalse(core.candidate_path(project, "line1", demo=False).exists())
        self.assertTrue(backends.transcribe(project, manifest, "whisper")["errors"])
        simulated = backends.transcribe(project, manifest, "demo")
        self.assertEqual(simulated["completed"], 1, simulated)
        evidence = core.list_evidence(project, "line1", demo=True)[0]
        self.assertTrue(evidence["demo"])
        self.assertTrue(evidence["details"]["simulated"])
        self.assertNotEqual(evidence["transcript"], self.target)
        self.assertEqual(backends.transcribe(project, manifest, "demo")["skipped"], 1)

    def test_whisper_blind_local_transcription_cache_and_new_model_identity(self):
        weight = self.root / "local.pt"
        weight.write_bytes(b"mock model version 1")
        project, manifest = self.generated_project({"whisper": {"model_path": "local.pt", "device": "cpu", "language": "zh"}})
        calls, loads = [], []

        class Model:
            def transcribe(inner, audio, **kwargs):
                calls.append((audio, kwargs))
                return {"text": "仅由伪模型返回的独立结果"}

        def load_model(path, **kwargs):
            loads.append((path, kwargs))
            return Model()

        with patch.object(backends, "_package_version", return_value="test-version"), \
             patch.dict("sys.modules", {"whisper": types.SimpleNamespace(load_model=load_model)}):
            report = backends.transcribe(project, manifest, "whisper")
            self.assertEqual(report["completed"], 1, report)
            self.assertEqual(backends.transcribe(project, manifest, "whisper")["skipped"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(loads), 1)
            self.assertEqual(loads[0][0], str(weight))
            self.assertEqual(Path(calls[0][0]), core.candidate_path(project, "line1"))
            self.assertNotIn("initial_prompt", calls[0][1])
            self.assertNotIn("hotwords", calls[0][1])
            self.assertNotIn(self.target, str(calls))
            self.assertFalse(calls[0][1]["condition_on_previous_text"])
            weight.write_bytes(b"mock model version 2")
            self.assertEqual(backends.transcribe(project, manifest, "whisper")["completed"], 1)
        evidence = core.list_evidence(project, "line1")
        self.assertEqual(len(evidence), 2)
        self.assertNotEqual(evidence[0]["model_id"], evidence[1]["model_id"])
        self.assertTrue(all(item["details"]["blind"] and not item["demo"] for item in evidence))

    def test_sensevoice_local_only_and_preserves_raw_tags(self):
        directory = self.root / "sense"
        directory.mkdir()
        (directory / "configuration.json").write_text("{}", encoding="utf-8")
        (directory / "model.pt").write_bytes(b"mock sensevoice")
        project, manifest = self.generated_project({"sensevoice": {"model_dir": "sense", "device": "cpu"}})
        loads, calls = [], []

        class Model:
            def generate(inner, **kwargs):
                calls.append(kwargs)
                return [{"text": "<|zh|><|NEUTRAL|>独立识别结果<|Speech|>"}]

        def AutoModel(**kwargs):
            loads.append(kwargs)
            return Model()

        with patch.object(backends, "_package_version", return_value="test-version"), \
             patch.dict("sys.modules", {"funasr": types.SimpleNamespace(AutoModel=AutoModel)}):
            report = backends.transcribe(project, manifest, "sensevoice")
        self.assertEqual(report["completed"], 1, report)
        self.assertEqual(loads[0]["model"], str(directory))
        self.assertIs(loads[0]["trust_remote_code"], False)
        self.assertIs(loads[0]["disable_update"], True)
        self.assertEqual(set(calls[0]), {"input", "language", "use_itn", "batch_size_s"})
        evidence = core.list_evidence(project, "line1")[0]
        self.assertEqual(evidence["transcript"], "独立识别结果")
        self.assertIn("<|NEUTRAL|>", evidence["details"]["raw_transcript"])

    def test_asr_failure_not_automatically_repeated(self):
        (self.root / "local.pt").write_bytes(b"mock model")
        project, manifest = self.generated_project({"whisper": {"model_path": "local.pt"}})
        with patch.object(backends, "_package_version", return_value="test"), \
             patch.object(backends, "_load_asr", side_effect=RuntimeError("simulated load failure")) as loader:
            self.assertTrue(backends.transcribe(project, manifest, "whisper")["errors"])
            self.assertTrue(backends.transcribe(project, manifest, "whisper")["errors"])
            self.assertEqual(loader.call_count, 1)
        self.assertEqual(core.list_evidence(project, "line1"), [])

    def test_remote_model_name_and_prompt_config_rejected(self):
        project, manifest = self.generated_project({"whisper": {"model_path": "large-v3"}})
        self.assertTrue(backends.transcribe(project, manifest, "whisper")["errors"])
        project["asr"]["whisper"]["initial_prompt"] = self.target
        self.assertTrue(backends.transcribe(project, manifest, "whisper")["errors"])


if __name__ == "__main__":
    unittest.main()
