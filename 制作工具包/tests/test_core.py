"""测试错误放行和错绑路径，而不是重复实现的字符串断言。"""
import array
import json
from pathlib import Path
import tempfile
import unittest
import wave

from voicekit import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nodes = [dict(id="line1", kind="line", speaker="向导", display_text="不要去三号门。",
                           reading_text="不要去三号门。", voice="narrator", source_ref="原创示例:1", next_id="end"),
                      dict(id="end", kind="end")]
        self.config = dict(project_id="test-project", title="原创样例", source="source.jsonl", work_dir="work",
                           output_dir="output", entry_id="line1", voices={"narrator": {
                               "reference_audio": "reference.wav", "prompt_text": "原创参考"}},
                           tts={"backend": "local-example", "engine_id": "test-model-sha-123", "temperature": 1},
                           asr={}, routes_confirmed=True)
        self.wav(self.root / "reference.wav")
        self.save_source()

    def wav(self, path, silent=False):
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(array.array("h", [0 if silent else (500 if i % 2 else -500)
                                                 for i in range(1600)]).tobytes())

    def save_source(self):
        (self.root / "source.jsonl").write_text("\n".join(json.dumps(n, ensure_ascii=False) for n in self.nodes), encoding="utf-8")

    def project(self):
        core.write_json(self.root / "project.json", self.config)
        return core.load_project(self.root / "project.json")

    def candidate(self, project, demo=False):
        path = self.root / "generated.wav"
        self.wav(path)
        return core.register_candidate(project, "line1", path, demo=demo)

    def test_negation_numbers_repetition_are_not_similarity_passes(self):
        for actual in ("要去三号门。", "不要去四号门。", "不要去三号门三号门。"):
            self.assertEqual(core.classify_evidence("不要去三号门。", [actual])["status"], "needs_review")
        self.assertEqual(core.classify_evidence("不要去三号门。", ["不要去三号门"])["status"], "supported")
        for expected, actual in (("-5", "5"), ("3.14", "314"), ("A-B", "AB"),
                                 ("1/4", "14"), ("3:14", "314"), ("30%", "30"),
                                 ("A", "a"), ("x²", "x2")):
            with self.subTest(expected=expected, actual=actual):
                self.assertEqual(core.classify_evidence(expected, [actual])["status"], "needs_review")

    def test_graph_rejects_duplicate_missing_unreachable_cycle_and_false_merge(self):
        cases = [self.nodes + [self.nodes[0]],
                 [dict(self.nodes[0], next_id="absent"), self.nodes[1]],
                 self.nodes + [dict(id="orphan", kind="end")],
                 [dict(self.nodes[0], next_id="line1"), self.nodes[1]]]
        for nodes in cases:
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                core.validate_source(nodes, "line1", {"narrator": {}})
        nodes = [dict(id="q", kind="choice", options=[
                    dict(id="a", label="左", target_id="line1", path_id="a", merge_id="merge", verified=True),
                    dict(id="b", label="右", target_id="line2", path_id="b", merge_id="merge", verified=True)]),
                 dict(self.nodes[0], path_id="a", next_id="end"),
                 dict(self.nodes[0], id="line2", path_id="b", next_id="merge"),
                 dict(id="merge", kind="merge", next_id="end"), self.nodes[1]]
        with self.assertRaises(ValueError):
            core.validate_source(nodes, "q", {"narrator": {}})

    def test_case_only_ids_are_rejected_for_windows_portability(self):
        nodes = [dict(self.nodes[0], next_id="LINE1"),
                 dict(self.nodes[0], id="LINE1"), self.nodes[1]]
        with self.assertRaisesRegex(ValueError, "忽略大小写"):
            core.validate_source(nodes, "line1", {"narrator": {}})
        for option_id, path_id in (("A", "right_path"), ("b", "LEFT_PATH")):
            choice = dict(id="q", kind="choice", options=[
                dict(id="a", label="左", target_id="line1", path_id="left_path", merge_id="merge", verified=True),
                dict(id=option_id, label="右", target_id="line2", path_id=path_id, merge_id="merge", verified=True)])
            graph = [choice, dict(self.nodes[0], path_id="left_path", next_id="merge"),
                     dict(self.nodes[0], id="line2", path_id=path_id, next_id="merge"),
                     dict(id="merge", kind="merge", next_id="end"), self.nodes[1]]
            with self.subTest(option_id=option_id, path_id=path_id), self.assertRaisesRegex(ValueError, "忽略大小写"):
                core.validate_source(graph, "q", {"narrator": {}})

    def test_paths_and_foreign_directory_rejected(self):
        for bad in ("../escape", "/tmp/escape", "C:\\outside", "work/../../escape"):
            self.config["work_dir"] = bad
            with self.subTest(path=bad), self.assertRaises(ValueError):
                self.project()
        self.config["work_dir"] = "work"
        (self.root / "work").mkdir()
        with self.assertRaises(ValueError):
            core.prepare(self.project())

    def test_bindings_preserve_old_candidates_and_ignore_review_changes(self):
        p = self.project()
        first = core.prepare(p)
        candidate = self.candidate(p)
        p["asr"] = {"model": "different-review-model"}
        self.assertEqual(core.prepare(p)["manifest_id"], first["manifest_id"])
        p["tts"]["temperature"] = 0.6
        with self.assertRaises(ValueError):
            core.get_candidate(p, "line1")
        second = core.prepare(p)
        self.assertNotEqual(first["manifest_id"], second["manifest_id"])
        self.assertTrue(Path(candidate["file"]).is_file())

    def test_wrong_binding_and_changed_audio_rejected(self):
        p = self.project()
        core.prepare(p)
        candidate = self.candidate(p)
        meta = Path(candidate["file"]).with_suffix(".json")
        bad = dict(candidate, binding_sha256="0" * 64)
        core.write_json(meta, bad)
        with self.assertRaises(ValueError):
            core.get_candidate(p, "line1")
        core.write_json(meta, candidate)
        with Path(candidate["file"]).open("ab") as handle:
            handle.write(b"changed")
        with self.assertRaises(ValueError):
            core.get_candidate(p, "line1")

    def test_reference_change_requires_prepare(self):
        p = self.project()
        core.prepare(p)
        with (self.root / "reference.wav").open("ab") as handle:
            handle.write(b"new")
        with self.assertRaises(ValueError):
            core.load_manifest(p)

    def test_real_engine_identity_cannot_be_placeholder(self):
        self.config["tts"]["engine_id"] = "REPLACE_WITH_YOUR_MODEL_SHA256"
        with self.assertRaises(ValueError):
            core.prepare(self.project())

    def test_acceptance_is_explicit_and_evidence_bound(self):
        p = self.project()
        core.prepare(p)
        self.candidate(p)
        core.record_evidence(p, "line1", "要去三号门", "test-asr-v1")
        with self.assertRaises(ValueError):
            core.accept(p, "line1", "未经核对不能通过")
        with self.assertRaises(ValueError):
            core.record_evidence(p, "line1", "不要去三号门", "test-asr-v1")
        # 新的独立模型确实提供全文支持后才允许普通采纳。
        core.record_evidence(p, "line1", "不要去三号门", "test-asr-v2")
        core.accept(p, "line1", "独立第二路全文支持，未人耳听审")
        pack = core.export_pack(p)
        self.assertTrue(pack.is_file())
        evidence_file = next((core.candidate_path(p, "line1").parent / "evidence").glob("*.json"))
        value = core.read_json(evidence_file)
        value["line_id"] = "wrong"
        core.write_json(evidence_file, value)
        with self.assertRaises(ValueError):
            core.export_pack(p)

    def test_demo_never_becomes_real_adoption_or_real_export(self):
        self.config["tts"] = {"backend": "demo"}
        self.config["voices"]["narrator"]["reference_audio"] = "missing.wav"
        p = self.project()
        core.prepare(p)
        self.candidate(p, demo=True)
        with self.assertRaises(ValueError):
            core.accept(p, "line1", "演示不能算真人配音")
        with self.assertRaises(ValueError):
            core.export_pack(p)
        pack = core.export_demo(p)
        self.assertIn("DEMO_NOT_VOICE", str(pack))
        self.assertTrue(core.read_json(pack)["voicekit"]["demo"])

    def test_wav_silence_and_physical_truncation(self):
        path = self.root / "silent.wav"
        self.wav(path, silent=True)
        with self.assertRaises(ValueError):
            core.inspect_wav(path)
        self.wav(path)
        data = path.read_bytes()
        path.write_bytes(data[:-20])
        with self.assertRaises(ValueError):
            core.inspect_wav(path)

    def test_routes_confirmation_required_for_real_export(self):
        p = self.project()
        core.prepare(p)
        self.candidate(p)
        core.record_evidence(p, "line1", "不要去三号门", "asr-v1")
        core.accept(p, "line1", "单路全文支持")
        p["routes_confirmed"] = False
        with self.assertRaises(ValueError):
            core.export_pack(p)


if __name__ == "__main__":
    unittest.main()
