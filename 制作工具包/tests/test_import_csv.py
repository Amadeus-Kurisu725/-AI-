"""CSV 导入边界测试：不丢分支、不覆盖、不伪造原文或源 ID。"""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from voicekit import core


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "import_csv.py"
SPEC = importlib.util.spec_from_file_location("voicekit_import_csv", SCRIPT)
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


class ImportCsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.csv"
        self.output = self.root / "imported.jsonl"

    def save(self, rows=None, extra=(), bom=False):
        rows = rows or [{"id": "source-a", "speaker": "旁白", "text": "原创正文。",
                         "voice": "narrator", "reading_text": "", "source_ref": "original/source-a"}]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=[*importer.REQUIRED_COLUMNS, *extra])
        writer.writeheader()
        writer.writerows(rows)
        raw = stream.getvalue().encode("utf-8-sig" if bom else "utf-8")
        self.source.write_bytes(raw)
        return raw

    def nodes(self):
        return [json.loads(line) for line in self.output.read_text(encoding="utf-8").splitlines()]

    def assert_clean(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".csv-import-*")), [])

    def test_bom_quotes_multiline_source_and_core_compatibility(self):
        text = '他说："灯已经亮了。"\n请沿海岸继续走。'
        rows = [{"id": "scene-original", "speaker": "旁白", "text": text, "voice": "narrator",
                 "reading_text": "", "source_ref": 'source/"chapter",entry\ncontinuation'},
                {"id": "another-stable-id", "speaker": "旁白", "text": "旅程结束。", "voice": "narrator",
                 "reading_text": "旅程结束。", "source_ref": "  original/kept-spaces  "}]
        raw = self.save(rows, bom=True)
        result = importer.import_csv(self.source, self.output)
        nodes = self.nodes()
        self.assertEqual(nodes[0]["display_text"], text)
        self.assertEqual(nodes[0]["reading_text"], text)
        self.assertEqual(nodes[0]["reading_change_reason"], "无")
        self.assertEqual([n["source_ref"] for n in nodes[:-1]], [r["source_ref"] for r in rows])
        self.assertEqual([n["id"] for n in nodes[:-1]], [r["id"] for r in rows])
        self.assertEqual(nodes[0]["next_id"], rows[1]["id"])
        self.assertEqual(nodes[1]["next_id"], importer.END_ID)
        self.assertEqual(result["input_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((result["source_row_count"], result["line_count"], result["node_count"]), (2, 2, 3))
        self.assertEqual(nodes[-1]["import_metadata"], result)
        self.assertNotIn(str(self.root), json.dumps(result))
        core.validate_source(nodes, rows[0]["id"], {"narrator": {}})

    def test_rejects_branch_columns_without_partial_output(self):
        for field in ("path_id", "options", "branch", "next_id", "targetId", "merge_id", "condition"):
            with self.subTest(field=field):
                self.save(extra=[field])
                content = self.source.read_text(encoding="utf-8")
                # 非空结构值即拒绝，无论取值是否看起来像线性信息。
                rows = list(csv.DictReader(io.StringIO(content)))
                rows[0][field] = "route-a"
                self.save(rows, extra=[field])
                with self.assertRaisesRegex(ValueError, "仅支持线性故事"):
                    importer.import_csv(self.source, self.output)
                self.assert_clean()

    def test_duplicate_id_and_unsafe_ids_rejected(self):
        self.save()
        row = next(csv.DictReader(io.StringIO(self.source.read_text(encoding="utf-8"))))
        self.save([row, dict(row)])
        with self.assertRaisesRegex(ValueError, "重复 ID"):
            importer.import_csv(self.source, self.output)
        self.assert_clean()
        for bad in ("../escape", "a/b", "NUL", "CON.txt", "tail.", " has-space", importer.END_ID):
            with self.subTest(id=bad):
                self.save([dict(row, id=bad)])
                with self.assertRaises(ValueError):
                    importer.import_csv(self.source, self.output)
                self.assert_clean()

    def test_existing_output_is_not_overwritten(self):
        self.save()
        original = b"existing file must stay unchanged\n"
        self.output.write_bytes(original)
        with self.assertRaises(FileExistsError):
            importer.import_csv(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".csv-import-*")), [])

    def test_competing_creator_is_not_overwritten(self):
        self.save()
        original_link = importer.os.link

        def race(source, target):
            Path(target).write_bytes(b"another process")
            return original_link(source, target)

        with mock.patch.object(importer.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                importer.import_csv(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"another process")
        self.assertEqual(list(self.root.glob(".csv-import-*")), [])

    def test_publish_failure_cleans_temporary_file(self):
        self.save()
        with mock.patch.object(importer.os, "link", side_effect=OSError("模拟不支持硬链接")):
            with self.assertRaises(OSError):
                importer.import_csv(self.source, self.output)
        self.assert_clean()

    def test_changed_reading_requires_reason_and_preserves_original(self):
        row = {"id": "stable-042", "speaker": "地图员", "text": "灯号042。", "voice": "character",
               "reading_text": "灯号零四二。", "source_ref": "original/stable-042"}
        self.save([row])
        with self.assertRaisesRegex(ValueError, "reading_change_reason"):
            importer.import_csv(self.source, self.output)
        self.assert_clean()
        self.save([dict(row, reading_change_reason="无")], extra=["reading_change_reason"])
        with self.assertRaises(ValueError):
            importer.import_csv(self.source, self.output)
        self.assert_clean()
        self.save([dict(row, reading_change_reason="编号逐位读出")], extra=["reading_change_reason"])
        metadata = importer.import_csv(self.source, self.output)
        self.assertEqual(self.nodes()[0]["display_text"], row["text"])
        self.assertEqual(self.nodes()[0]["reading_change_reason"], "编号逐位读出")
        self.assertEqual(metadata["reading_changed_count"], 1)

    def test_empty_text_and_malformed_csv_rejected(self):
        self.save([{"id": "empty", "speaker": "旁白", "text": "   ", "voice": "narrator",
                    "reading_text": "", "source_ref": "original/empty"}])
        with self.assertRaisesRegex(ValueError, "text 不能为空"):
            importer.import_csv(self.source, self.output)
        self.assert_clean()
        for content in ('id,speaker,text,voice,reading_text,source_ref\na,旁白,"未闭合',
                        'id,speaker,text,voice,reading_text,source_ref\na,旁白,正文,narrator\n',
                        'id,id,speaker,text,voice,reading_text,source_ref\n'):
            self.source.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                importer.import_csv(self.source, self.output)
            self.assert_clean()


if __name__ == "__main__":
    unittest.main()
