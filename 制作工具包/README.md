# 剧情配音制作工具包

这是一套从《战双帕弥什》配音工程经验中重新整理的**可移植教学实现**。它把台本检查、配置冻结、逐句生成、盲回读、审核登记和章节打包串成可运行的流程，供其他游戏爱好者学习和适配。

完整制作历史、设备、失败案例与审核方法见[制作全流程与其他游戏适配指南](../制作全流程与其他游戏适配指南.md)。

默认演示只需要 **Python 3.11 或以上**，不下载模型、不调用在线服务、不需要独立显卡。真实配音需要你另外准备本地 GPT-SoVITS 服务、可使用的参考录音和本地识别模型。

## 这份代码覆盖什么

- 使用 JSONL 导入原创示例或自己的已核对台本，保持显示原文、朗读派生、来源和剧情位置。
- 验证单章单小节中的对白、选择、汇合和结束，拒绝缺失目标、错误分支和未解释的循环。
- 冻结文本、参考声音及 TTS 配置身份，保留旧版本候选，在同一份冻结清单内校验并复用断点。
- 通过 GPT-SoVITS `api_v2.py` 的 `/tts` 接口生成 WAV，不自动切换服务端权重。
- 选择本地 openai-whisper 或 SenseVoice 做独立盲回读，保留原始转写与模型身份。
- 输出审核建议，单独登记正式采纳；只有已采纳的当前波形能够导出真实包。
- 导出 `pack.json` 和相对音频路径，保留两条分支及汇合节点。

这是新整理的精简工具，**不是原工程全部生产脚本的无条件搬迁**。原工程中的网页采集、游戏专用资源解析、完整人机容差规则、多路 GPU 调度、字幕截图、进程文字读取和手机界面不包含在本包中。原播放器源码与资源工具见[来源与迁移边界](docs/来源与迁移边界.md)。

## 先用一分钟看清示例

在这个 `制作工具包` 文件夹打开终端，执行：

```console
python --version
python -m voicekit --config config.example.json demo
```

Windows 也可以双击 `运行离线示例.cmd`。这个入口只执行上述演示，不启动后台生产。

示例中有一段原创虚构剧情、两条互斥分支、一个汇合点，以及编号 `007` 的朗读派生。两条分支各有一句相同文字，位置 ID 分别保存，用来说明“文本相同不等于剧情节点相同”。

程序会生成短提示音和明确标记的模拟识别记录，输出中包含 `demo: true`。**提示音不会朗读台词，模拟记录不是 ASR 识别结果。** 演示目录与真实候选隔离；演示包标题及元数据有 DEMO 标记，真实采纳与真实导出会拒绝它。

运行后查看命令打印的包路径，以及 `work_demo/`、`output_demo/`。再次执行演示应复用通过文件身份检查的结果。不要把这些生成目录上传到源码仓库。

如果只想检查台本和冻结配置：

```console
python -m voicekit --config config.example.json prepare
```

## 真实制作的准备

1. 复制 `config.gpt-sovits.example.json` 为 `config.local.json`。
2. 将自己的已核对台本写成 JSONL，并修改 `source`。
3. 为每个声音配置填写本机参考音频、准确 `prompt_text` 和 `prompt_lang`。原始示例不附带角色声音。
4. 将 `tts.engine_id` 改成你实际服务的代码版本与权重身份记录，不能保留示例占位值。
5. 使用匹配版本的 GPT-SoVITS 独立启动本地 API。默认地址是 `http://127.0.0.1:9880/tts`，以你的服务实际配置为准。
6. 准备至少一个本地 ASR 模型，并在独立兼容的 Python 环境中安装其运行依赖。
7. 实际检查本章路线后将 `routes_confirmed` 设为 `true`。改这个字段只是记录你的检查结果，不会替你证明游戏真实路线。

配置中的相对文件路径以**配置文件所在目录**为基准。台本、参考音频、任务和输出目录必须使用项目内相对路径，例如 `references/narrator.wav`，不接受 `..` 或越界链接。ASR 模型可使用已经存在的本地绝对路径或相对路径。私人配置默认被 `.gitignore` 忽略。

GPT-SoVITS 服务需要能够读取参考文件。默认使用与工具同机的绝对路径；若另配 `server_ref_audio_path`，请自行确保它指向相同内容，工具不能隔空验证服务端磁盘。

### TTS 环境

按 [GPT-SoVITS 官方说明](https://github.com/RVC-Boss/GPT-SoVITS)准备服务及模型。本包只通过 HTTP 请求使用它，不会在你的生产目录里安装依赖、修改默认配置或启动/停止服务。

请求固定为完整 WAV 返回，不使用流式碎片。示例配置采用 `cut5` 让服务按其标点策略切分；这与复盘中原生产的“外层分段＋cut0”不同。本工具不实现原生产全部长句切分策略，较长文本应先用小样验证。

HTTP 接口没有提供本工具所需的完整运行权重证明，所以 `engine_id` 需要维护者真实记录并在权重变化后更新。工具将它纳入缓存身份，但不能仅凭一个相同字符串判断服务端实际上没有切模型。

### ASR 环境

选一个后端即可；需要第二份独立证据时再配置另一个。

| 后端 | Python 包与外部条件 | 本地模型配置 |
|---|---|---|
| Whisper | `openai-whisper`、匹配的 PyTorch，音频加载需要可调用的 FFmpeg | `asr.whisper.model_path` 指向本地 `.pt` |
| SenseVoice | 兼容的 `funasr` 及模型要求的依赖 | `asr.sensevoice.model_dir` 指向完整本地模型目录 |

可在独立环境按上游要求安装，例如选择 Whisper 时使用 `python -m pip install openai-whisper`，选择 SenseVoice 时按 [SenseVoice](https://github.com/QwenAudio/SenseVoice)及 [FunASR](https://github.com/modelscope/FunASR)说明安装。模型本身需另行准备，工具不替你下载。

本包没有提供一份声称适用所有显卡的 CUDA 锁文件。真实环境通过后请保存你的包版本、驱动、权重校验值和服务版本；默认演示与单元测试不需要这些可选依赖。

## 真实制作命令

先检查并冻结输入，然后生成首轮候选：

```console
python -m voicekit --config config.local.json prepare
python -m voicekit --config config.local.json generate
```

生成命令只处理当前清单，在同一份冻结清单中，已存在的正确绑定和 WAV 会复用。不同文本、声音或引擎配置会产生新清单，旧目录保留。**首版不自动把旧清单中未变化行的候选和采纳迁移到新清单**；改动前先完成小样核对，避免把原工程的跨版本增量能力误认为已经移植。它不默认多候选竞争，也不为失败句无限重试。

选一个或两个本地模型盲回读：

```console
python -m voicekit --config config.local.json transcribe --model whisper
python -m voicekit --config config.local.json transcribe --model sensevoice
python -m voicekit --config config.local.json review
```

ASR 请求不携带目标台词、提示答案或热词。只有完成识别后才由审核层比较。原始转写不为了通过而改写，已有同模型、同波形证据优先复用。

教学版自动建议采用简单、可解释的**单路完整文字归一匹配**。任一路满足即可给出 `supported`；其他差异保留 `needs_review`，不称为“确定错音”，也不自动重配。这里没有移植原工程全部同音、语义容差和多路位置互补规则。可以在理解并测试之后扩展 `classify_evidence`，修改审核逻辑不应重做 TTS。

核对一条后正式采纳，示例中的 `guard` 应换成你实际的 ID：

```console
python -m voicekit --config config.local.json accept --line guard --reason "现有完整回读支持正文，绑定与波形已核对"
```

如果属于已核实的可接受差异，明确写出原因并使用 `--allow-difference`。只有真的听过当前音频才加 `--human-listened`，不能把语义判断伪装成听审：

```console
python -m voicekit --config config.local.json accept --line guard --allow-difference --reason "填写本句差异位置、为什么可以接受，以及所依据的证据"
```

全部口播已正式采纳并确认路线后导出：

```console
python -m voicekit --config config.local.json export
```

导出会复核当前台本、选用和音频身份，不只看保存的通过标志。缺采纳、文件变动或绑定不一致会阻止真实导出。最终仍要在你的播放器与游戏路径中验证实际加载、路线和播放；这一步不由 JSON 文件存在自动代替。

## 台本格式

每行一个 JSON 对象，见 [完整示例](examples/story.jsonl)。文件使用 UTF-8。

| 字段 | 用途 |
|---|---|
| `id` | 稳定、安全的 ASCII 节点 ID；不要用可变数组下标 |
| `kind` | `line`、`choice`、`merge`、`gap`、`end` |
| `speaker` | 原角色或已核实名称 |
| `display_text` | 保留给用户看的原文 |
| `reading_text` | `line` 的完整朗读文本 |
| `reading_change_reason` | 派生原因；无修改写“无” |
| `source_ref` | 可追溯的原材料位置 |
| `voice` | 配置中实际存在的声音名称 |
| `path_id` | 共同线为空，分支填写独立路径 ID |
| `next_id` | 下一节点，终止节点不填 |
| `options` | 选择项的 ID、标签、目标、路径、汇合点和核实状态 |

本版为了让边界清楚，只接收一个入口的单章单小节、无循环、单层分支剧情图，不支持嵌套选择。复杂游戏的循环菜单、运行时变量、跨片段跳转和性别条件需要单独适配，不能用假连接绕过检查。

### 从线性 CSV 导入台本

从工具包根目录运行：

```console
python tools/import_csv.py --input examples/linear.csv --output examples/imported.jsonl
```

CSV 必需列为 `id,speaker,text,voice,reading_text,source_ref`。`id` 必须是来源中的稳定 ID；导入器不会按行号生成对白 ID。`reading_text` 留空时采用 `text`，理由记为“无”；修改朗读文本时，必须另填 `reading_change_reason`。原文和每条 `source_ref` 原样保留。

原创 CSV 示例入口为 `beacon_arrival`。使用导入结果前，在自己的配置中同时设置 `source` 为 `examples/imported.jsonl`、`entry_id` 为 `beacon_arrival`，并核对 `voices` 与 CSV 的 `voice` 对应。更换输入示例时使用新的工作及输出目录，保留之前的结果。

该脚本仅处理线性故事，把台词按 CSV 顺序连为共同线并补 `csv_import_end` 结束节点。`path_id/options/branch/next_id` 等结构列出现非空值便拒绝；未知列也拒绝，避免静默丢失信息。分支故事应按现有 JSONL 示例单独适配。输入 SHA256、台词数、节点数与入口 ID 保存在结束节点的 `import_metadata`，不记录本机路径。

输出已存在便拒绝，失败不会留下半成品；原子新增需要目标文件系统支持硬链接，常见 NTFS/ext4 支持。

## 文件与恢复

- 原始台本不被生成命令改写。
- `prepare` 保存冻结清单；更改输入后必须重新准备。
- 候选、证据与正式采纳按绑定身份保存，旧记录继续保留。
- 对同一个工作目录串行执行命令；本版不是多进程生产调度器。
- 中断后先检查已经保存的结果，再重新运行同一阶段复用有效成果。失败或结果不明的尝试会阻止相同条件重复推理；强制结束进程还可能留下运行锁。首版没有自动恢复这些未完成调用的命令，需要维护者先核对尝试记录、进程和已写文件，保留证据后处理。不要靠随意改 `engine_id` 或删除记录制造重试。
- 手动删除候选、修改原始 ASR 或篡改通过标志不是修复流程；有新问题应记录原因并保留原证据。

## 测试与验收

```console
python -m unittest discover -s tests -v
```

测试使用临时目录、提示音和模拟 HTTP 服务，不联系真实模型服务。重点检查文本与身份绑定、否定和数字差异、路径与剧情图、示例与正式结果隔离、API 参数及错误处理。

此次交付的 34 项测试和完整演示已通过，具体范围见[本次验证说明](docs/本次验证说明.md)。

这证明工具在所列边界内的程序行为，不证明某个模型对你的游戏、角色或语言达到了配音质量。真实 TTS、真实 ASR、用户听感和目标游戏中的实际播放需要分别验证。

## 许可与来源

沿用仓库[非商业使用许可证](LICENSE.md)。本包示例文本为此次新写的虚构内容，不包含《战双帕弥什》剧情或角色录音。模型、参考声音及第三方代码遵守各自许可。

上游软件和原项目对应关系、未包含能力见[来源与迁移边界](docs/来源与迁移边界.md)。
