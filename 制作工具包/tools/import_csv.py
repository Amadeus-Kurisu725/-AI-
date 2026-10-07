"""把有来源稳定 ID 的 CSV 导入为共同线 JSONL；只支持线性故事。

Python 3.11+ 标准库。用法（从工具包根目录执行）：
  python tools/import_csv.py --input examples/linear.csv --output examples/imported.jsonl

必需列：id,speaker,text,voice,reading_text,source_ref。
可选列：reading_change_reason；改动朗读文本时必须填写真实理由。
id 必须来自源资料的稳定标识，脚本不会用行号制造对白 ID。
原文、朗读文本和 source_ref 均保留原值；空朗读文本使用原文并记理由“无”。
含分支/跳转等结构字段的非空内容一律拒绝；未知列也拒绝，避免丢失信息。
合成的终止节点 ID 为 csv_import_end，输入不得占用。
末尾 end 节点的 import_metadata 记录输入字节 SHA256、计数、入口等信息，
不记录本机路径。它不是原剧情节点，也不是一条需要配音的台词。

先完整校验，再原子创建输出；不覆盖已有文件。发布使用同目录硬链接，
目标文件系统不支持硬链接时会报错并清理临时文件，不退回覆盖写入。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile


REQUIRED_COLUMNS = ("id", "speaker", "text", "voice", "reading_text", "source_ref")
OPTIONAL_COLUMNS = {"reading_change_reason"}
END_ID = "csv_import_end"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}
# 去除下划线、空格、连字符并转小写后比较，兼容常见字段拼法。
STRUCTURAL_COLUMNS = {
    "path", "pathid", "paths", "option", "options", "optionid", "branch", "branches",
    "branchid", "next", "nextid", "target", "targetid", "merge", "mergeid", "route",
    "routes", "routeid", "choice", "choices", "choiceid", "condition", "conditions",
    "jump", "jumpto", "goto", "kind", "type", "trigger", "triggers",
}


def _structural(column: str) -> bool:
    return re.sub(r"[_\s-]", "", column).casefold() in STRUCTURAL_COLUMNS


def _safe_id(value: str, label: str) -> str:
    if (not SAFE_ID.fullmatch(value) or value.endswith(".")
            or value.split(".", 1)[0].upper() in RESERVED):
        raise ValueError(f"{label} 必须是安全 ASCII ID，且不能是 Windows 保留文件名")
    return value


def parse_csv(raw: bytes) -> tuple[list[dict], dict]:
    """纯解析与校验；成功前不写任何文件。"""
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("输入必须是 UTF-8 CSV（允许 BOM）") from exc
    reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
    try:
        columns = reader.fieldnames
        if not columns:
            raise ValueError("CSV 缺少表头")
        if len(set(columns)) != len(columns):
            raise ValueError("CSV 表头存在重复列")
        missing = set(REQUIRED_COLUMNS) - set(columns)
        if missing:
            raise ValueError("CSV 缺少必需列：" + ", ".join(sorted(missing)))
        unknown = [column for column in columns
                   if column not in REQUIRED_COLUMNS and column not in OPTIONAL_COLUMNS
                   and not _structural(column)]
        if unknown:
            raise ValueError("不支持的列（不会忽略潜在结构信息）：" + ", ".join(unknown))
        structure = [column for column in columns if _structural(column)]
        nodes: list[dict] = []
        seen: set[str] = set()
        for row in reader:
            location = f"CSV 第 {reader.line_num} 行附近"
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{location} 列数与表头不一致")
            occupied = [column for column in structure if row[column].strip()]
            if occupied:
                raise ValueError(f"{location} 含结构字段 {', '.join(occupied)}；"
                                 "仅支持线性故事，不能自动压平分支或跳转")
            key = _safe_id(row["id"], f"{location} id")
            if key == END_ID:
                raise ValueError(f"{location} 占用了导入器保留 ID {END_ID}")
            if key in seen:
                raise ValueError(f"{location} 重复 ID：{key}")
            seen.add(key)
            for field in ("speaker", "text", "voice", "source_ref"):
                if not row[field].strip():
                    raise ValueError(f"{location} {field} 不能为空")
            _safe_id(row["voice"], f"{location} voice")
            reading = row["reading_text"] if row["reading_text"].strip() else row["text"]
            if not any(char.isalnum() for char in reading):
                raise ValueError(f"{location} 只有符号；请另建 gap 等非口播节点")
            reason = "无"
            if reading != row["text"]:
                reason = row.get("reading_change_reason", "")
                if not reason.strip() or reason.strip() == "无":
                    raise ValueError(f"{location} 朗读文本与正文不同，必须填写 reading_change_reason")
            nodes.append({
                "id": key, "kind": "line", "speaker": row["speaker"],
                "display_text": row["text"], "reading_text": reading,
                "voice": row["voice"], "path_id": "", "source_ref": row["source_ref"],
                "reading_change_reason": reason,
            })
    except csv.Error as exc:
        raise ValueError(f"CSV 格式无效，第 {reader.line_num} 行附近：{exc}") from exc
    if not nodes:
        raise ValueError("CSV 没有台词")
    for index, node in enumerate(nodes):
        node["next_id"] = nodes[index + 1]["id"] if index + 1 < len(nodes) else END_ID
    metadata = {
        "schema_version": 1,
        "importer": "voicekit-csv-linear-1",
        "source_format": "csv",
        "linear_only": True,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "source_row_count": len(nodes),
        "line_count": len(nodes),
        "node_count": len(nodes) + 1,
        "reading_changed_count": sum(node["display_text"] != node["reading_text"] for node in nodes),
        "source_ids_preserved": True,
        "entry_id": nodes[0]["id"],
        "synthetic_end_id": END_ID,
    }
    nodes.append({"id": END_ID, "kind": "end", "speaker": "", "display_text": "线性导入结束",
                  "path_id": "", "import_metadata": metadata})
    return nodes, metadata


def import_csv(input_path: str | Path, output_path: str | Path) -> dict:
    """验证后原子新增一个 JSONL；返回不含本机路径的导入元数据。"""
    input_path, output_path = Path(input_path), Path(output_path)
    if os.path.lexists(output_path):
        raise FileExistsError("输出已存在，拒绝覆盖；请更换输出文件名")
    nodes, metadata = parse_csv(input_path.read_bytes())
    payload = "".join(json.dumps(node, ensure_ascii=False, allow_nan=False) + "\n" for node in nodes)
    if not output_path.parent.is_dir():
        raise ValueError("输出目录不存在，请先建立目录")
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".csv-import-", suffix=".tmp", dir=output_path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link 是新增目录项：若别的进程抢先建立目标，会失败而不会替换它。
        os.link(temporary, output_path)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仅导入线性故事 CSV，遇分支信息拒绝；保留源稳定 ID。")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 CSV（允许 BOM）")
    parser.add_argument("--output", required=True, type=Path, help="新增 JSONL 文件，禁止覆盖")
    args = parser.parse_args(argv)
    try:
        metadata = import_csv(args.input, args.output)
    except (ValueError, OSError) as exc:
        print(f"导入失败：{exc}", file=sys.stderr)
        return 1
    print(f"已导入线性台词 {metadata['line_count']} 句，合计 {metadata['node_count']} 个节点。")
    print(f"配置 entry_id 应设为：{metadata['entry_id']}；source 应指向生成的 JSONL。")
    print("输入 SHA256 与计数已写入末尾 end 节点的 import_metadata。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
