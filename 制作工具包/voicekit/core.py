"""教学用的可追溯配音核心。只依赖 Python 3.11 标准库。

这不是原生产系统的迁移；审核只做单路全文归一相等，差异留待明确裁决。
所有项目路径限制在配置文件所在目录，真实与 DEMO 产物隔离。
"""
from __future__ import annotations

import array
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
import tempfile
import unicodedata
import wave
from datetime import datetime, timezone

CORE_VERSION = "voicekit-teaching-1"
REVIEW_VERSION = "whole-normalized-equality-1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path, value):
    """原子替换单个 JSON；调用方负责传入本项目已验证路径。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _id(value, label="ID"):
    if (not isinstance(value, str) or not SAFE_ID.fullmatch(value) or value.endswith(".")
            or value.split(".", 1)[0].upper() in RESERVED):
        raise ValueError(f"{label} 必须是安全 ASCII 标识（字母、数字、_、-、.），不能是保留文件名")
    return value


def _inside(base, path):
    base, path = Path(base).resolve(), Path(path).resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError(f"路径越界或覆盖项目根目录：{path}")
    return path


def _relative(base, value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须填写项目内相对路径")
    portable = value.replace("\\", "/")
    if Path(portable).is_absolute() or PureWindowsPath(value).is_absolute() or ":" in portable:
        raise ValueError(f"{label} 不能使用绝对路径")
    if ".." in Path(portable).parts:
        raise ValueError(f"{label} 不允许 .. 路径")
    return _inside(base, Path(base) / portable)


def load_project(path):
    config_path = Path(path).resolve(strict=True)
    project = read_json(config_path)
    if not isinstance(project, dict):
        raise ValueError("配置必须是 JSON 对象")
    project = dict(project)
    base = config_path.parent
    project["project_id"] = _id(project.get("project_id"), "project_id")
    if not isinstance(project.get("title"), str) or not project["title"].strip():
        raise ValueError("title 不能为空")
    _id(project.get("entry_id"), "entry_id")
    for key in ("source", "work_dir", "output_dir"):
        project[key] = str(_relative(base, project.get(key), key))
    work, output, source = (Path(project[k]) for k in ("work_dir", "output_dir", "source"))
    if (work == output or work.is_relative_to(output) or output.is_relative_to(work)
            or source.is_relative_to(work) or source.is_relative_to(output)
            or config_path.is_relative_to(work) or config_path.is_relative_to(output)):
        raise ValueError("源文件、配置、工作目录和输出目录必须分离")
    if not isinstance(project.get("voices"), dict) or not project["voices"]:
        raise ValueError("voices 必须包含至少一个声音配置")
    voices = {}
    for key, raw in project["voices"].items():
        _id(key, "voice")
        if not isinstance(raw, dict):
            raise ValueError("声音配置必须是对象")
        voice = dict(raw)
        if voice.get("reference_audio"):
            voice["reference_audio"] = str(_relative(base, voice["reference_audio"], "reference_audio"))
            reference = Path(voice["reference_audio"])
            if reference.is_relative_to(work) or reference.is_relative_to(output):
                raise ValueError("参考音频不能放在生成工作目录或输出目录")
        voices[key] = voice
    project["voices"] = voices
    for key in ("tts", "asr"):
        if not isinstance(project.get(key, {}), dict):
            raise ValueError(f"{key} 必须是配置对象")
        project[key] = dict(project.get(key, {}))
    project["_base_dir"], project["_config_path"] = str(base), str(config_path)
    project["_owner_id"] = digest({"config": str(config_path), "project_id": project["project_id"]})
    return project


def validate_source(project_or_nodes, entry_id=None, voices=None):
    """验证一个显式 DAG；无出口、未知选项或循环必须先修源，不猜路线。"""
    if isinstance(project_or_nodes, dict):
        project = project_or_nodes
        source = _inside(project["_base_dir"], project["source"])
        nodes = []
        with source.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    nodes.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"源文件第 {line_number} 行不是有效 JSON") from exc
        entry_id, voices = project["entry_id"], project["voices"]
    else:
        nodes = project_or_nodes
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("源节点不能为空")
    nodes = json.loads(json.dumps(nodes, ensure_ascii=False))
    by_id, edges = {}, {}
    folded_node_ids = set()
    all_option_ids, all_path_ids = set(), set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("每个节点必须是对象")
        key = _id(node.get("id"), "node.id")
        if key.casefold() in folded_node_ids:
            raise ValueError(f"重复节点 ID（跨平台忽略大小写检查）：{key}")
        folded_node_ids.add(key.casefold())
        by_id[key] = node
        kind = node.get("kind")
        if kind not in {"line", "choice", "merge", "gap", "end"}:
            raise ValueError(f"未知节点类型：{key}")
        if node.get("path_id"):
            _id(node["path_id"], "path_id")
        targets = []
        if kind == "line":
            for field in ("display_text", "reading_text", "speaker", "source_ref"):
                if not isinstance(node.get(field), str) or not node[field].strip():
                    raise ValueError(f"{key} 缺少 {field}")
            if not any(ch.isalnum() for ch in node["reading_text"]):
                raise ValueError(f"{key} 只有符号；请明确标记为 gap 等非口播节点")
            if (node["display_text"] != node["reading_text"]
                    and not str(node.get("reading_change_reason", "")).strip()):
                raise ValueError(f"{key} 修改朗读文本必须记录理由")
            if node.get("voice") not in (voices or {}):
                raise ValueError(f"{key} 声音配置不存在")
        if kind in {"line", "merge"}:
            targets.append(_id(node.get("next_id"), f"{key}.next_id"))
        elif kind == "choice":
            if node.get("next_id") is not None:
                raise ValueError(f"{key} choice 不能另设自动 next_id")
            options = node.get("options")
            if not isinstance(options, list) or len(options) < 2:
                raise ValueError(f"{key} choice 至少需要两个明确选项")
            option_ids, path_ids = set(), set()
            for option in options:
                if not isinstance(option, dict):
                    raise ValueError(f"{key} 选项格式错误")
                oid = _id(option.get("id"), "option.id")
                pid = _id(option.get("path_id"), "option.path_id")
                folded_oid, folded_pid = oid.casefold(), pid.casefold()
                if (folded_oid in option_ids or folded_pid in path_ids
                        or folded_oid in all_option_ids or folded_pid in all_path_ids):
                    raise ValueError(f"{key} 重复选项 ID 或分支 path_id（跨平台忽略大小写检查）")
                option_ids.add(folded_oid)
                path_ids.add(folded_pid)
                all_option_ids.add(folded_oid)
                all_path_ids.add(folded_pid)
                if not isinstance(option.get("label"), str) or not option["label"].strip():
                    raise ValueError(f"{key} 选项缺少 label")
                if option.get("verified") is not True:
                    raise ValueError(f"{key} 存在未核实路线；请先核实或改成明确 gap")
                targets.append(_id(option.get("target_id"), "option.target_id"))
                _id(option.get("merge_id"), "option.merge_id")
        else:
            if node.get("next_id") is not None or node.get("options"):
                raise ValueError(f"{key} end/gap 必须明确终止，不能猜测后续")
            if kind == "gap" and not str(node.get("reason") or node.get("display_text") or "").strip():
                raise ValueError(f"{key} gap 必须说明缺口或人工定位原因")
        edges[key] = targets
    if entry_id not in by_id:
        raise ValueError("entry_id 指向不存在的节点")
    if not any(node["kind"] == "line" for node in nodes):
        raise ValueError("配音包至少需要一个有明确内容的 line 节点")
    for key, targets in edges.items():
        for target in targets:
            if target not in by_id:
                raise ValueError(f"{key} 指向不存在的节点 {target}")
    # Kahn 检查避免长章递归溢出；随后从入口独立核可达性。
    indegree = dict.fromkeys(by_id, 0)
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1
    stack = [key for key, value in indegree.items() if value == 0]
    visited_count = 0
    while stack:
        key = stack.pop()
        visited_count += 1
        for target in edges[key]:
            indegree[target] -= 1
            if indegree[target] == 0:
                stack.append(target)
    if visited_count != len(nodes):
        raise ValueError("教学版不支持循环；请拆开场景或标记 gap")
    reached, stack = set(), [entry_id]
    while stack:
        key = stack.pop()
        if key not in reached:
            reached.add(key)
            stack.extend(edges[key])
    if reached != set(by_id):
        raise ValueError("存在入口不可达节点：" + ", ".join(sorted(set(by_id) - reached)))
    branch_members = {}
    for node in nodes:
        if node["kind"] != "choice":
            continue
        for option in node["options"]:
            merge = option["merge_id"]
            if merge not in by_id or by_id[merge]["kind"] != "merge":
                raise ValueError(f"{node['id']} 汇合节点不存在或不是 merge")
            seen, pending = set(), [option["target_id"]]
            while pending:
                key = pending.pop()
                if key == merge or key in seen:
                    continue
                seen.add(key)
                current = by_id[key]
                if current["kind"] in {"choice", "merge"}:
                    raise ValueError("教学版只支持单层明确分支；嵌套分支需另建适配器")
                if not edges[key]:
                    raise ValueError(f"{node['id']} 分支未到声明的汇合点")
                if current.get("path_id") != option["path_id"]:
                    raise ValueError(f"{key} 分支 path_id 与选项不一致")
                if key in branch_members and branch_members[key] != option["path_id"]:
                    raise ValueError("分支正文在正式汇合前发生交叉")
                branch_members[key] = option["path_id"]
                pending.extend(edges[key])
    for node in nodes:
        if node.get("path_id") and branch_members.get(node["id"]) != node["path_id"]:
            raise ValueError(f"{node['id']} 带有未归属于明确选项正文的 path_id")
    return nodes


def _claim_directory(project, field):
    root = _inside(project["_base_dir"], project[field])
    marker = root / ".voicekit-owner.json"
    if root.exists():
        if not root.is_dir() or not marker.is_file():
            raise ValueError(f"拒绝写入已有且非本工具创建的目录：{root}")
        if read_json(marker) != {"owner_id": project["_owner_id"], "kind": field}:
            raise ValueError("工作/输出目录归属不符")
    else:
        root.mkdir(parents=True, exist_ok=False)
        write_json(marker, {"owner_id": project["_owner_id"], "kind": field})
    return root


def _snapshot(project):
    nodes = validate_source(project)
    tts = dict(project["tts"])
    demo = tts.get("backend") == "demo"
    if not demo and not str(tts.get("engine_id", "")).strip():
        raise ValueError("真实制作必须填写固定的 tts.engine_id（版本/权重身份）")
    engine_id = str(tts.get("engine_id", "")).strip()
    if not demo and (re.search(r"(?i)^(replace(?:\b|_)|todo(?:\b|_)|changeme|your[-_]|fill[-_]|demo[-_])", engine_id)
                     or any(word in engine_id for word in ("待填写", "请替换", "占位"))):
        raise ValueError("真实 tts.engine_id 仍是示例占位值；请填写实际固定版本与权重身份")
    voices = {}
    for key, raw in project["voices"].items():
        voice = dict(raw)
        reference = voice.get("reference_audio")
        if reference:
            path = _inside(project["_base_dir"], reference)
            exists = path.is_file()
        else:
            exists = False
        if not exists and not demo:
            raise ValueError(f"{key} 缺少真实参考音频")
        if not demo and not str(voice.get("prompt_text") or voice.get("reference_transcript") or "").strip():
            raise ValueError(f"{key} 缺少参考音频准确转录")
        voice["reference_sha256"] = sha256(path) if exists else None
        voices[key] = voice
    source_hash = sha256(project["source"])
    # 审核规则、ASR配置、routes_confirmed不属于生成缓存身份。
    payload = {"core_version": CORE_VERSION, "project_id": project["project_id"],
               "title": project["title"], "entry_id": project["entry_id"], "nodes": nodes,
               "source_sha256": source_hash, "voices": voices, "tts": tts, "demo": demo}
    manifest_id = digest(payload)
    lines = []
    for node in nodes:
        if node["kind"] == "line":
            row = dict(node)
            row["voice_config"] = voices[row["voice"]]
            row["binding_sha256"] = digest({"project_id": project["project_id"], "row": row,
                                               "tts": tts, "core_version": CORE_VERSION})
            lines.append(row)
    return {**payload, "manifest_id": manifest_id, "lines": lines}


def prepare(project):
    manifest = _snapshot(project)
    root = _claim_directory(project, "work_dir")
    folder = _inside(root, root / "bindings" / manifest["manifest_id"])
    target = folder / "manifest.json"
    if target.exists():
        if read_json(target) != manifest:
            raise ValueError("冻结 manifest 内容已变化，拒绝覆盖")
    else:
        write_json(target, manifest)
    write_json(root / "current.json", {"manifest_id": manifest["manifest_id"]})
    return manifest


def load_manifest(project):
    root = _claim_directory(project, "work_dir")
    current = read_json(root / "current.json")
    key = current.get("manifest_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("当前 manifest 指针格式错误")
    path = _inside(root, root / "bindings" / key / "manifest.json")
    manifest = read_json(path)
    if manifest != _snapshot(project):
        raise ValueError("源文、参考、引擎或冻结清单已变化；请重新 prepare，旧绑定会保留")
    return manifest


def line_context(project, line_id):
    _id(line_id, "line_id")
    manifest = load_manifest(project)
    row = next((row for row in manifest["lines"] if row["id"] == line_id), None)
    if row is None:
        raise ValueError(f"当前清单没有口播行 {line_id}")
    root = Path(project["work_dir"])
    folder = _inside(root, root / "bindings" / manifest["manifest_id"] / "lines" / line_id)
    return manifest, row, folder


def candidate_path(project, line_id, demo=False):
    _, _, folder = line_context(project, line_id)
    path = _inside(project["work_dir"], folder / ("demo" if demo else "real") / "candidate.wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def inspect_wav(path):
    """完整解码 PCM16 WAV；不把语速或时长猜测当作正文截断证据。"""
    try:
        with wave.open(str(path), "rb") as handle:
            channels, width, rate, frames = (handle.getnchannels(), handle.getsampwidth(),
                                            handle.getframerate(), handle.getnframes())
            if width != 2 or channels not in (1, 2) or rate <= 0 or handle.getcomptype() != "NONE":
                raise ValueError("只支持单/双声道 PCM16 WAV")
            data = handle.readframes(frames)
        if not frames or len(data) != frames * channels * width:
            raise ValueError("WAV 为空或文件物理截断")
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        peak = max(abs(value) for value in samples)
        if peak == 0:
            raise ValueError("WAV 是数字静音")
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
        return {"status": "valid", "frames": frames, "sample_rate": rate, "channels": channels,
                "duration": frames / rate, "peak": peak / 32768, "rms": rms,
                "content_verified": False}
    except (wave.Error, EOFError) as exc:
        raise ValueError("不是可完整读取的 PCM16 WAV") from exc


def _binding(manifest, row, audio_hash, demo):
    return {"manifest_id": manifest["manifest_id"], "line_id": row["id"],
            "binding_sha256": row["binding_sha256"], "audio_sha256": audio_hash, "demo": bool(demo)}


def register_candidate(project, line_id, wav_path, demo=False, details=None):
    manifest, row, _ = line_context(project, line_id)
    if manifest["demo"] and not demo:
        raise ValueError("DEMO 配置不得登记真实候选")
    wav_path = _inside(project["_base_dir"], wav_path)
    signal = inspect_wav(wav_path)
    audio_hash = sha256(wav_path)
    target = candidate_path(project, line_id, demo)
    meta = target.with_suffix(".json")
    if meta.exists():
        old = get_candidate(project, line_id, demo)
        if old["audio_sha256"] != audio_hash or old.get("details", {}) != (details or {}):
            raise ValueError("该绑定已有候选；拒绝覆盖旧波形或生成身份，请新建配置绑定")
        return old
    if target.exists() and target.resolve() != wav_path.resolve() and sha256(target) != audio_hash:
        raise ValueError("候选文件已存在且不同，拒绝覆盖")
    if target.resolve() != wav_path.resolve():
        temp = target.with_suffix(".writing.wav")
        shutil.copyfile(wav_path, temp)
        if sha256(temp) != audio_hash:
            raise ValueError("复制音频哈希不符")
        os.replace(temp, target)
    value = {**_binding(manifest, row, audio_hash, demo), "file": str(target),
             "signal": signal, "details": details or {}, "created_at": now()}
    write_json(meta, value)
    return value


def get_candidate(project, line_id, demo=False):
    manifest, row, _ = line_context(project, line_id)
    target = candidate_path(project, line_id, demo)
    value = read_json(target.with_suffix(".json"))
    audio_hash = sha256(target)
    expected = _binding(manifest, row, audio_hash, demo)
    if any(value.get(key) != item for key, item in expected.items()) or value.get("file") != str(target):
        raise ValueError("候选绑定或音频哈希错误")
    inspect_wav(target)
    if manifest["demo"] and not demo:
        raise ValueError("DEMO 清单不能成为真实候选")
    return value


def list_evidence(project, line_id, demo=False):
    candidate = get_candidate(project, line_id, demo)
    folder = _inside(project["work_dir"], Path(candidate["file"]).parent / "evidence")
    values = []
    for path in sorted(folder.glob("*.json")):
        _inside(project["work_dir"], path)
        value = read_json(path)
        for key in ("manifest_id", "line_id", "binding_sha256", "audio_sha256", "demo"):
            if value.get(key) != candidate[key]:
                raise ValueError("ASR 证据与当前候选不匹配")
        if not isinstance(value.get("transcript"), str) or not isinstance(value.get("model_id"), str):
            raise ValueError("ASR 证据缺少原始转写或模型身份")
        checksum = value.get("evidence_sha256")
        if checksum != digest({k: v for k, v in value.items() if k != "evidence_sha256"}):
            raise ValueError("ASR 冻结证据被修改")
        values.append(value)
    return values


def record_evidence(project, line_id, transcript, model_id, demo=False, details=None):
    if not isinstance(transcript, str) or not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("ASR 必须提供字符串转写和非空模型身份")
    candidate = get_candidate(project, line_id, demo)
    if any(value["model_id"] == model_id for value in list_evidence(project, line_id, demo)):
        raise ValueError("同一波形、同一模型已有回读；请复用，不重复识别")
    value = {key: candidate[key] for key in ("manifest_id", "line_id", "binding_sha256", "audio_sha256", "demo")}
    value.update(transcript=transcript, model_id=model_id, details=details or {}, created_at=now(),
                 method="simulated-demo" if demo else "blind-transcription")
    value["evidence_sha256"] = digest(value)
    name = digest({"audio": candidate["audio_sha256"], "model": model_id}) + ".json"
    write_json(Path(candidate["file"]).parent / "evidence" / name, value)
    return value


def _normalize(text):
    # 只忽略窄清单里的句读/引号。负号、小数点、分数、代号连字符、
    # 百分号、冒号等可能携带意义，全部保留；宁可留下疑点。
    # NFKC 也可能将 ①/1 或 ²/2 折叠，故只使用保守 NFC。
    ignored = frozenset("，。！？；、‘’“”「」『』〈〉《》,!?;\"'")
    text = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in text if not ch.isspace() and ch not in ignored)


def classify_evidence(reading_text, transcripts):
    target = _normalize(reading_text)
    if not target:
        return {"status": "needs_review", "reason": "没有可核对正文"}
    for value in transcripts:
        actual = value.get("transcript") if isinstance(value, dict) else value
        if isinstance(actual, str) and _normalize(actual) == target:
            return {"status": "supported", "reason": "单路完整归一文本相等；这不是人耳听审"}
    return {"status": "needs_review", "reason": "全文支持不足，差异仅为疑点；教学版未迁移历史语义/近音容差，不自动补配"}


def review(project, demo=False):
    manifest = load_manifest(project)
    rows = []
    for row in manifest["lines"]:
        try:
            candidate = get_candidate(project, row["id"], demo)
            evidence = list_evidence(project, row["id"], demo)
            decision = ({"status": "needs_review", "reason": "DEMO 占位声音与模拟回读不证明任何台词内容"}
                        if demo else classify_evidence(row["reading_text"], evidence))
            rows.append({"line_id": row["id"], **decision, "audio_sha256": candidate["audio_sha256"],
                         "evidence_count": len(evidence)})
        except (FileNotFoundError, ValueError) as exc:
            rows.append({"line_id": row["id"], "status": "needs_review", "reason": str(exc)})
    result = {"manifest_id": manifest["manifest_id"], "demo": bool(demo), "review_version": REVIEW_VERSION,
              "lines": rows, "formal_adoptions": 0, "created_at": now()}
    write_json(Path(project["work_dir"]) / "bindings" / manifest["manifest_id"] /
               ("review-demo.json" if demo else "review-real.json"), result)
    return result


def accept(project, line_id, reason, human_listened=False, allow_difference=False):
    if not isinstance(reason, str) or len(reason.strip()) < 3:
        raise ValueError("正式采纳必须填写具体理由，至少三个字符")
    manifest, row, _ = line_context(project, line_id)
    if manifest["demo"]:
        raise ValueError("DEMO 永远不能正式采纳")
    candidate = get_candidate(project, line_id)
    evidence = list_evidence(project, line_id)
    if not evidence:
        raise ValueError("缺少原始回读证据，不可正式采纳")
    decision = classify_evidence(row["reading_text"], evidence)
    if decision["status"] != "supported" and not allow_difference:
        raise ValueError("仍有内容疑点；需独立核实并显式 allow_difference，不能自动采纳")
    result = {key: candidate[key] for key in ("manifest_id", "line_id", "binding_sha256", "audio_sha256", "demo")}
    result.update(reason=reason.strip(), human_listened=bool(human_listened),
                  allow_difference=bool(allow_difference), decision=decision, review_version=REVIEW_VERSION,
                  evidence_sha256=[value["evidence_sha256"] for value in evidence], accepted_at=now())
    result["acceptance_sha256"] = digest(result)
    target = Path(candidate["file"]).parent / "acceptance.json"
    if target.exists():
        old = read_json(target)
        # 相同事实的重复命令只返回原回执，不改历史时间和理由。
        comparable = ("manifest_id", "line_id", "binding_sha256", "audio_sha256", "demo", "reason",
                      "human_listened", "allow_difference", "evidence_sha256")
        if any(old.get(key) != result[key] for key in comparable):
            raise ValueError("已有正式采纳回执且本次不同；教学版不覆盖历史采纳")
        return old
    write_json(target, result)
    return result


def _accepted(project, row, candidate):
    value = read_json(Path(candidate["file"]).parent / "acceptance.json")
    for key in ("manifest_id", "line_id", "binding_sha256", "audio_sha256", "demo"):
        if value.get(key) != candidate[key]:
            raise ValueError("正式采纳回执与当前候选绑定不符")
    if value.get("demo") or value.get("acceptance_sha256") != digest(
            {k: v for k, v in value.items() if k != "acceptance_sha256"}):
        raise ValueError("正式采纳回执不完整或已变更")
    evidence = list_evidence(project, row["id"])
    available = {item["evidence_sha256"]: item for item in evidence}
    used = value.get("evidence_sha256", [])
    if not used or any(key not in available for key in used):
        raise ValueError("正式采纳所依赖的原始回读已缺失或变更")
    decision = classify_evidence(row["reading_text"], [available[key] for key in used])
    if decision["status"] != "supported" and not value.get("allow_difference"):
        raise ValueError("正式采纳没有全文支持或明确差异裁决")
    return value


def export_pack(project, demo=False):
    manifest = load_manifest(project)
    if not demo and (manifest["demo"] or project.get("routes_confirmed") is not True):
        raise ValueError("真实导出需要真实制作配置及 routes_confirmed=true 的结构核实")
    selections = {}
    for row in manifest["lines"]:
        candidate = get_candidate(project, row["id"], demo)
        acceptance = None if demo else _accepted(project, row, candidate)
        selections[row["id"]] = (candidate, acceptance)
    root = _claim_directory(project, "output_dir")
    folder = _inside(root, root / ("DEMO_NOT_VOICE" if demo else "正式包") / manifest["manifest_id"])
    folder.mkdir(parents=True, exist_ok=True)
    title = ("[DEMO 非配音] " if demo else "") + project["title"]
    nodes = []
    for raw in manifest["nodes"]:
        node = {"id": raw["id"], "sectionId": "single-section", "kind": raw["kind"],
                "pathId": raw.get("path_id", ""), "speaker": raw.get("speaker", ""),
                "text": raw.get("display_text", ""), "rawText": raw.get("display_text", "")}
        if raw.get("next_id"):
            node["nextId"] = raw["next_id"]
        if raw["kind"] == "choice":
            node["options"] = [{"id": option["id"], "label": option["label"], "targetId": option["target_id"],
                                "pathId": option["path_id"], "mergeId": option["merge_id"], "verified": True}
                               for option in raw["options"]]
        if raw["kind"] == "gap":
            node["text"] = raw.get("reason") or raw.get("display_text")
        if raw["kind"] == "line":
            candidate, acceptance = selections[raw["id"]]
            relative = "audio/" + candidate["audio_sha256"] + ".wav"
            target = _inside(folder, folder / relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if sha256(target) != candidate["audio_sha256"]:
                    raise ValueError("已有导出音频被修改，拒绝覆盖")
            else:
                shutil.copyfile(candidate["file"], target)
            if sha256(target) != candidate["audio_sha256"]:
                raise ValueError("导出音频复制验证失败")
            inspect_wav(target)
            node.update(audio=relative, audioStatus="demo-not-voice" if demo else "bound",
                        audioEvidence={"sha256": candidate["audio_sha256"], "bindingSha256": candidate["binding_sha256"],
                                       "demo": bool(demo), "listeningReviewed": bool(acceptance and acceptance["human_listened"])})
        nodes.append(node)
    pack = {"schemaVersion": 1, "id": ("demo-" if demo else "voicekit-") + project["project_id"],
            "title": title, "delivery": {"status": "DEMO_NOT_VOICE" if demo else "teaching-explicitly-accepted",
                                         "label": "占位声，不是真实配音" if demo else "逐条明确采纳", "revision": manifest["manifest_id"]},
            "chapters": [{"id": "single-chapter", "title": title,
                          "sections": [{"id": "single-section", "title": title, "startId": manifest["entry_id"]}]}],
            "nodes": nodes, "voicekit": {"demo": bool(demo), "manifest_id": manifest["manifest_id"],
                                          "note": "新整理的教学精简版；不代表历史生产系统完整迁移或人耳全量验收"}}
    target = folder / "pack.json"
    if target.exists() and read_json(target) != pack:
        raise ValueError("相同冻结版本的导出包已有不同内容，拒绝覆盖")
    write_json(target, pack)
    write_json(folder / "verification.json", {"manifest_id": manifest["manifest_id"], "demo": bool(demo),
                                             "pack_sha256": sha256(target), "audio_nodes": len(selections),
                                             "graph_validated": True, "all_pcm_read": True,
                                             "player_runtime_verified": False})
    return target


def export_demo(project):
    return export_pack(project, demo=True)
