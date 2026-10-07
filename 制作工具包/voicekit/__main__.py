"""不启动后台任务；所有模型请求均须显式执行对应命令。"""
from __future__ import annotations

import argparse
import json
import sys

from . import core, backends


def require_completed(stage, result):
    if result.get("errors") or result.get("halted") or result.get("failed", 0):
        raise RuntimeError(f"{stage} 未完成，后续阶段已停止：" + json.dumps(result, ensure_ascii=False))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="剧情配音教学工具包（不会修改原生产工程）")
    parser.add_argument("--config", default="config.example.json", help="配置文件；路径相对于该文件解析")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare", help="检查台本与图结构，保存新的冻结清单")
    commands.add_parser("generate", help="调用配置中的TTS后端，复用校验有效的候选")
    transcribe = commands.add_parser("transcribe", help="对当前候选盲回读，不提供目标台词")
    transcribe.add_argument("--model", choices=("whisper", "sensevoice"), required=True)
    commands.add_parser("review", help="按已有证据生成审核建议，不自动采纳")
    accept = commands.add_parser("accept", help="正式登记一个候选；所有差异须说明理由")
    accept.add_argument("--line", required=True, dest="line_id")
    accept.add_argument("--reason", required=True)
    accept.add_argument("--allow-difference", action="store_true", help="明确采纳转写差异；不自动等于人耳听审")
    accept.add_argument("--human-listened", action="store_true", help="仅在确实听过当前文件时填写")
    commands.add_parser("export", help="核验正式选用后导出真实章节包")
    commands.add_parser("demo", help="离线提示音演示；不调用TTS/ASR，不作正式交付")
    args = parser.parse_args(argv)
    try:
        project = core.load_project(args.config)
        if args.command == "prepare":
            result = core.prepare(project)
        elif args.command == "demo":
            if project.get("tts", {}).get("backend") != "demo":
                raise ValueError("demo只接受backend=demo的配置；请使用config.example.json")
            manifest = core.prepare(project)
            generated = backends.generate(project, manifest, demo=True)
            require_completed("演示生成", generated)
            recognized = backends.transcribe(project, manifest, model="demo")
            require_completed("模拟记录", recognized)
            review = core.review(project, demo=True)
            require_completed("演示审核", review)
            exported = core.export_pack(project, demo=True)
            result = {"demo": True, "notice": "仅提示音与模拟记录，不是台词配音或真实识别",
                      "generation": generated, "simulated_evidence": recognized,
                      "review": review, "export": exported}
        elif args.command == "generate":
            if project.get("tts", {}).get("backend") == "demo":
                raise ValueError("演示配置请执行demo；真实生成先配置本地模型服务与参考音频")
            result = backends.generate(project, core.load_manifest(project), demo=False)
        elif args.command == "transcribe":
            result = backends.transcribe(project, core.load_manifest(project), model=args.model)
        elif args.command == "review":
            result = core.review(project)
        elif args.command == "accept":
            result = core.accept(project, args.line_id, args.reason,
                                 human_listened=args.human_listened,
                                 allow_difference=args.allow_difference)
        else:
            result = core.export_pack(project)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if isinstance(result, dict) and (result.get("errors") or result.get("halted") or result.get("failed", 0)):
            return 2
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, ImportError) as exc:
        print(json.dumps({"error": str(exc), "command": args.command}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
