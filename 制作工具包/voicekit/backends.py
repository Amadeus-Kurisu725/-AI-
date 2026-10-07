"""可选本地推理适配器；导入本模块只需要 Python 标准库。

模型和 GPT-SoVITS 服务由使用者自行准备。本模块不下载模型、不启动服务、
不切换服务权重，也不自动重试失败请求。demo 只产生提示音，绝非文字配音。
"""

from __future__ import annotations

import importlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave

from . import core


_TTS_PARAMETERS = {
    "top_k", "top_p", "temperature", "text_split_method", "batch_size",
    "batch_threshold", "split_bucket", "speed_factor", "fragment_interval",
    "seed", "parallel_infer", "repetition_penalty", "sample_steps",
    "super_sampling", "overlap_length", "min_chunk_length",
}
_TTS_DEFAULTS = {
    "top_k": 15, "top_p": 1.0, "temperature": 1.0,
    "text_split_method": "cut5", "batch_size": 1,
    "batch_threshold": 0.75, "split_bucket": True, "speed_factor": 1.0,
    "fragment_interval": 0.3, "seed": -1, "parallel_infer": True,
    "repetition_penalty": 1.35, "sample_steps": 32,
    "super_sampling": False, "overlap_length": 2, "min_chunk_length": 16,
}


def _error(result, line_id, exc):
    result["errors"].append({"line_id": line_id, "error": str(exc)})
    result["halted"] = True


def _result(**extra):
    return {"completed": 0, "skipped": 0, "errors": [], "halted": False, **extra}


def _active_row(project, manifest, row):
    current, actual, _ = core.line_context(project, row["id"])
    if (current["manifest_id"] != manifest["manifest_id"]
            or actual["binding_sha256"] != row["binding_sha256"]):
        raise ValueError("传入清单不是当前绑定；请重新读取 prepare 的结果")
    return actual


def _path(project, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("必须填写本地文件或目录路径")
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path(project["_base_dir"]) / path).resolve()


def _atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".voicekit-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _wave_info(data):
    """完整读取 PCM WAV，拒绝 JSON、空文件、空帧及截断响应。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate, frames = (
                wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()
            )
            if wav.getcomptype() != "NONE" or channels < 1 or width not in (1, 2, 3, 4):
                raise ValueError("只接受未压缩 PCM WAV")
            if rate < 1 or frames < 1:
                raise ValueError("WAV 没有有效音频帧")
            samples = wav.readframes(frames)
            if len(samples) != frames * channels * width:
                raise ValueError("WAV 音频帧被截断")
            if not any(samples):
                raise ValueError("WAV 仅含零值静音")
            return {"channels": channels, "sample_width": width,
                    "sample_rate": rate, "frames": frames, "duration": frames / rate}
    except (wave.Error, EOFError) as exc:
        raise ValueError("响应不是可完整读取的 PCM WAV") from exc


def _demo_wave():
    out = io.BytesIO()
    rate, count = 16000, 4000
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        samples = []
        for index in range(count):
            envelope = min(1.0, index / 160, (count - 1 - index) / 160)
            samples.append(struct.pack("<h", round(3000 * envelope * math.sin(2 * math.pi * 440 * index / rate))))
        wav.writeframes(b"".join(samples))
    return out.getvalue()


def _tts_spec(project, manifest, row, demo):
    config = manifest.get("tts", project.get("tts", {}))
    backend = config.get("backend", "demo")
    demo = bool(demo or backend == "demo" or manifest.get("demo", False))
    if demo:
        return {"backend": "demo", "engine_id": "voicekit-tone-v1", "demo": True,
                "synthetic": True, "simulation": "440Hz提示音；没有朗读台词",
                "binding_sha256": row["binding_sha256"]}, None, None
    if backend != "gpt_sovits_api":
        raise ValueError("tts.backend 只支持 demo 或 gpt_sovits_api")
    engine = config.get("engine_id")
    if not isinstance(engine, str) or not engine.strip():
        raise ValueError("真实生成必须填写 tts.engine_id，固定服务版本和权重身份")
    url = config.get("api_url", "http://127.0.0.1:9880/tts")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("api_url 必须为不含账号密码的 HTTP(S) /tts 地址")
    if parsed.path.rstrip("/") != "/tts" or parsed.query or parsed.fragment:
        raise ValueError("api_url 只能指向 /tts，不能附加查询参数或控制端点")
    voice = row["voice_config"]
    reference = _path(project, voice.get("reference_audio"))
    if not reference.is_file():
        raise ValueError("本地 reference_audio 不存在")
    reference_hash = core.sha256(reference)
    if reference_hash != voice.get("reference_sha256"):
        raise ValueError("参考音频已变更，请重新 prepare；不能沿用旧绑定")
    parameters = config.get("parameters", {})
    if not isinstance(parameters, dict) or set(parameters) - _TTS_PARAMETERS:
        raise ValueError("tts.parameters 含不支持的字段，不能覆盖正文、参考音或输出格式")
    parameters = {**_TTS_DEFAULTS, **parameters}
    # 禁止 NaN/Infinity，避免服务端与身份摘要对同一参数作不同解释。
    json.dumps(parameters, allow_nan=False)
    payload = {
        **parameters, "text": row["reading_text"],
        "text_lang": config.get("text_lang", "zh"),
        "ref_audio_path": voice.get("server_ref_audio_path") or str(reference),
        "prompt_text": voice.get("prompt_text", voice.get("reference_transcript", "")),
        "prompt_lang": voice.get("prompt_lang", "zh"),
        "streaming_mode": False, "media_type": "wav",
    }
    spec = {"backend": backend, "engine_id": engine, "demo": False,
            "binding_sha256": row["binding_sha256"], "reference_sha256": reference_hash,
            "request": payload, "api_url": url,
            "engine_identity_source": "user-declared; HTTP不能自动证明服务权重未变"}
    return spec, reference, config


def _http_wave(config, spec):
    timeout = float(config.get("timeout_seconds", 180))
    limit = int(config.get("max_response_bytes", 100 * 1024 * 1024))
    if not 0 < timeout <= 3600 or not 44 <= limit <= 1024 * 1024 * 1024:
        raise ValueError("timeout_seconds 或 max_response_bytes 超出允许范围")
    request = urllib.request.Request(
        spec["api_url"], json.dumps(spec["request"], ensure_ascii=False, allow_nan=False).encode("utf-8"),
        {"Content-Type": "application/json", "Accept": "audio/wav"}, method="POST",
    )
    # 禁止重定向，确保不会在意外地址重发正文和参考音路径。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GPT-SoVITS 请求失败，HTTP {exc.code}；本次不会自动重试") from exc
    if len(data) > limit:
        raise ValueError("API 音频响应超过 max_response_bytes")
    _wave_info(data)
    return data


def _take_lock(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("同一工作单元存在运行锁；先核查已有进程/未完成记录，不要直接重复推理") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("active; if interrupted inspect the attempt record before recovery\n")
    return path


def generate(project, manifest, demo=False):
    """逐行生成首个候选；成功断点复用，失败或结果不明则停止，不自动重试。"""
    result = _result(demo=bool(demo or manifest.get("demo", False)))
    for provided in manifest["lines"]:
        line_id = provided["id"]
        lock = None
        attempt_path = None
        attempt = None
        temporary = None
        try:
            row = _active_row(project, manifest, provided)
            spec, reference, config = _tts_spec(project, manifest, row, demo)
            is_demo = spec["demo"]
            result["demo"] = is_demo
            generation_identity = core.digest(spec)
            destination = core.candidate_path(project, line_id, demo=is_demo)
            lock = _take_lock(destination.parent, ".generation.lock")
            try:
                saved = core.get_candidate(project, line_id, demo=is_demo)
            except FileNotFoundError:
                saved = None
                if destination.exists():
                    raise ValueError("存在未登记音频；请核查上次中断，不能自动重新生成")
            if saved is not None:
                if saved.get("details", {}).get("generation_identity") != generation_identity:
                    raise ValueError("已有候选的生成身份不同；请重新 prepare，不能覆盖已有候选")
                result["skipped"] += 1
                continue
            attempt_path = destination.parent / "generation_attempt.json"
            if attempt_path.exists():
                previous = core.read_json(attempt_path)
                raise ValueError(f"已有生成尝试（{previous.get('status', 'unknown')}）；禁止等价重试，请核查记录")
            attempt = {"status": "started", "line_id": line_id,
                       "binding_sha256": row["binding_sha256"],
                       "generation_identity": generation_identity, "demo": is_demo}
            core.write_json(attempt_path, attempt)
            data = _demo_wave() if is_demo else _http_wave(config, spec)
            audio_info = _wave_info(data)
            if reference is not None and core.sha256(reference) != spec["reference_sha256"]:
                raise ValueError("请求期间本地参考音频改变，不能登记候选")
            temporary = destination.parent / ".generated-response.wav"
            _atomic_bytes(temporary, data)
            details = {**spec, "generation_identity": generation_identity, "audio_info": audio_info}
            core.register_candidate(project, line_id, temporary, demo=is_demo, details=details)
            core.write_json(attempt_path, {**attempt, "status": "completed"})
            result["completed"] += 1
        except Exception as exc:
            if attempt is not None and attempt_path is not None:
                core.write_json(attempt_path, {**attempt, "status": "failed_or_unconfirmed", "error": str(exc)})
            _error(result, line_id, exc)
            break
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if lock is not None:
                lock.unlink(missing_ok=True)
    return result


def _package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"缺少可选依赖 {name}；请在独立模型环境安装后再运行") from exc


def _asr_spec(project, model):
    if model not in ("whisper", "sensevoice"):
        raise ValueError("真实 ASR 只支持 whisper 或 sensevoice；demo由独立演示命令处理")
    options = project.get("asr", {}).get(model, {})
    if not isinstance(options, dict):
        raise ValueError(f"asr.{model} 必须是配置对象")
    allowed = {"model_path", "model_dir", "device", "language"}
    if set(options) - allowed:
        raise ValueError("ASR 配置含不支持字段；盲转写禁止原文提示、hotwords 或 initial_prompt")
    device, language = options.get("device", "cpu"), options.get("language", "zh")
    if not isinstance(device, str) or not isinstance(language, str):
        raise ValueError("ASR device 和 language 必须是字符串")
    if model == "whisper":
        path = _path(project, options.get("model_path"))
        if not path.is_file() or path.suffix.lower() != ".pt":
            raise ValueError("Whisper model_path 必须指向已经存在的本地 .pt 权重，不接受远程模型名称")
        artifacts = [{"file": path.name, "sha256": core.sha256(path)}]
        package = _package_version("openai-whisper")
        inference = {"language": language, "temperature": 0, "condition_on_previous_text": False,
                     "fp16": device.startswith("cuda"), "verbose": None}
    else:
        path = _path(project, options.get("model_dir"))
        if not path.is_dir():
            raise ValueError("SenseVoice model_dir 必须是已存在的本地完整模型目录")
        # FunASR 默认配置名与权重名；不传仓库名称，不允许缺失后在线补文件。
        if not (path / "configuration.json").is_file() or not (path / "model.pt").is_file():
            raise ValueError("SenseVoice 本地目录须含 configuration.json 和 model.pt；先自行准备完整模型")
        artifacts = []
        for item in sorted(path.rglob("*")):
            if any(part in ("__pycache__", ".git") for part in item.relative_to(path).parts):
                continue
            if item.is_symlink():
                raise ValueError("模型目录不能包含符号链接；请使用明确的本地模型文件")
            if item.is_file():
                artifacts.append({"file": item.relative_to(path).as_posix(), "sha256": core.sha256(item)})
        package = _package_version("funasr")
        inference = {"language": language, "use_itn": False, "batch_size_s": 30}
    spec = {"backend": model, "package_version": package, "artifacts": artifacts,
            "device": device, "inference": inference, "blind": True,
            "adapter_version": "voicekit-local-asr-v1"}
    return spec, path


def _load_asr(model, path, spec):
    if model == "whisper":
        whisper = importlib.import_module("whisper")
        return whisper.load_model(str(path), device=spec["device"])
    funasr = importlib.import_module("funasr")
    return funasr.AutoModel(model=str(path), device=spec["device"],
                           trust_remote_code=False, disable_update=True, disable_pbar=True)


def _run_asr(model, loaded, audio, spec):
    if model == "whisper":
        response = loaded.transcribe(str(audio), **spec["inference"])
        if not isinstance(response, dict) or not isinstance(response.get("text"), str):
            raise ValueError("Whisper 未返回文本结构")
        return response["text"], {"raw_transcript": response["text"]}
    response = loaded.generate(input=str(audio), **spec["inference"])
    if not isinstance(response, list) or not response or any(
            not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in response):
        raise ValueError("SenseVoice 未返回文本结构")
    raw = "".join(item["text"] for item in response)
    transcript = re.sub(r"<\|[^|]+\|>", "", raw)
    return transcript, {"raw_transcript": raw, "raw_texts": [item["text"] for item in response]}


def transcribe(project, manifest, model="whisper"):
    """盲转写当前真实候选，保存原始文本，不比较目标原文、不自动采纳。"""
    if model == "demo":
        return _simulated_evidence(project, manifest)
    result = _result(model=model, demo=False)
    try:
        if manifest.get("demo") or manifest.get("tts", {}).get("backend") == "demo":
            raise ValueError("demo 提示音不能做真实台词回读；演示证据必须单独标记 simulated")
        spec, path = _asr_spec(project, model)
        identity = core.digest(spec)
        model_id = f"{model}:{identity}"
    except Exception as exc:
        _error(result, None, exc)
        return result
    loaded = None
    for row in manifest["lines"]:
        line_id = row["id"]
        lock = None
        attempt = None
        attempt_path = None
        try:
            _active_row(project, manifest, row)
            candidate = core.get_candidate(project, line_id, demo=False)
            if candidate.get("demo") or candidate.get("details", {}).get("synthetic"):
                raise ValueError("不能将演示/合成占位提示音当作真实台词识别")
            audio = core.candidate_path(project, line_id, demo=False)
            lock = _take_lock(audio.parent, f".asr-{identity}.lock")
            existing = core.list_evidence(project, line_id, demo=False)
            if any(item.get("model_id") == model_id for item in existing):
                result["skipped"] += 1
                continue
            attempt_path = audio.parent / "asr_attempts" / f"{identity}.json"
            if attempt_path.exists():
                raise ValueError("相同音频和模型已有未完成或失败尝试；禁止自动等价重试，请核查记录")
            attempt = {"status": "started", "line_id": line_id,
                       "audio_sha256": candidate["audio_sha256"], "model_id": model_id,
                       "model_identity": spec, "demo": False}
            core.write_json(attempt_path, attempt)
            if loaded is None:
                loaded = _load_asr(model, path, spec)
                # 确认模型加载期间磁盘权重和配置未被更新。
                fresh, _ = _asr_spec(project, model)
                if core.digest(fresh) != identity:
                    raise ValueError("模型加载期间本地文件改变，不能绑定本次回读")
            transcript, raw = _run_asr(model, loaded, audio, spec)
            if core.sha256(audio) != candidate["audio_sha256"]:
                raise ValueError("回读期间音频改变，不能保存证据")
            core.record_evidence(project, line_id, transcript, model_id, demo=False,
                                 details={"model_identity": spec, "blind": True, **raw})
            core.write_json(attempt_path, {**attempt, "status": "completed"})
            result["completed"] += 1
        except Exception as exc:
            if attempt is not None and attempt_path is not None:
                core.write_json(attempt_path, {**attempt, "status": "failed_or_unconfirmed", "error": str(exc)})
            _error(result, line_id, exc)
            break
        finally:
            if lock is not None:
                lock.unlink(missing_ok=True)
    return result


def _simulated_evidence(project, manifest):
    """仅演示证据流；固定说明文字不来自 ASR，也不复制目标台词。"""
    result = _result(model="demo", demo=True, simulated=True)
    if not manifest.get("demo") or manifest.get("tts", {}).get("backend") != "demo":
        _error(result, None, ValueError("模拟证据只允许在独立 demo 清单中生成"))
        return result
    model_id = "demo:voicekit-simulated-evidence-v1"
    for row in manifest["lines"]:
        try:
            _active_row(project, manifest, row)
            core.get_candidate(project, row["id"], demo=True)
            if any(item.get("model_id") == model_id for item in core.list_evidence(project, row["id"], demo=True)):
                result["skipped"] += 1
                continue
            core.record_evidence(
                project, row["id"], "[SIMULATED] 仅440Hz提示音，未执行语音识别，也未朗读台词。",
                model_id, demo=True,
                details={"simulated": True, "synthetic": True,
                         "simulation": "固定说明文字；未调用ASR，不作为正文正确证据"},
            )
            result["completed"] += 1
        except Exception as exc:
            _error(result, row["id"], exc)
            break
    return result
