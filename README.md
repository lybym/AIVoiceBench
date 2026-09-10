# AIVoiceBench

面向 AI 语音终端的录音分析与评测 Harness：录音 → Evidence → Events → Metrics → Semantic Evaluation → Findings → Human Verification → Regression。

## 先读产品需求

**[中心 PRD：范围、验收、逐项代码实现标识](docs/PRD.md)**。这是审阅和修改产品行为的唯一入口。[产品文档中心](docs/product/README.md) 集中保存旧需求来源和归档；[文档导航](docs/README.md) 区分架构、测试、指标、路线图和工作日志的职责。

Primary Workflow 为已有 WAV/MP3/M4A 录音导入分析；保留既有契约、Runner、ASR/Vosk、确定性引擎及后续 Audio Station/HIL。交付为 Docker 后端+前端、Windows 浏览器访问，无 Windows 安装包要求。完整真实录音 MVP 尚未验收，不把发布包或合成测试当成设备准确率证据。

## 当前代码与发布

本次文档审计 main 为 19d3a07；[v0.1.3 Release](https://github.com/lybym/AIVoiceBench/releases/tag/v0.1.3) 为 e3c2821，包含新版 Web、三种格式 Web 导入与模型管理。PR #43/#45 尚未合并，因此运行 main 源码与运行发布镜像可能不同。逐项区别见 PRD，不在 README 另建完成清单。

## Docker / Windows 浏览器

从 Release 下载镜像后在 PowerShell 中运行：

```powershell
docker load -i .\aivoicebench-v0.1.3.tar.gz
docker run -d --name aivoicebench -p 127.0.0.1:8000:8000 -v aivoicebench-output:/data/output -v aivoicebench-cache:/data/cache aivoicebench:v0.1.3
```

打开 http://localhost:8000 。升级已有容器须保留原有卷映射。模型管理适用于可信单用户部署，语音适配器显示“待接入”时不会自动调用；存储配置不代表连通性或准确性验证。

The highest-priority MVP imports an existing 5–20 minute WAV/MP3/M4A conversation recording and automatically produces a trustworthy, evidence-linked report. Existing TestCase, Timeline, Evidence, metrics, findings, Runner, ASR/Vosk and deterministic engine are retained. Audio Station/HIL becomes a later automation extension. See [migration and repository audit](docs/13-import-first-migration.md).

See `docs/` for architecture and roadmap, and `schemas/` for the canonical contracts.

## Docker and browser delivery

The final deliverable is a Docker-deployed backend and frontend, accessed through a Web UI from Windows browsers. A Windows executable or installer is not required. Supply Docker configuration, persistent storage, startup and browser usage instructions. Online ASR/TTS/LLM providers may be configured; local execution does not promise offline operation. A cloud service cluster is not required. The current CLI validates contracts and prepares local Run/audio artifacts with explicit measurement blockers; it is not yet a hardware runner or packaged application. See [local runner instructions](docs/09-local-runner.md).

The import-first milestone now also provides `python -m aivoicebench import recording.wav` (WAV/MP3/M4A), source preservation, canonical conversion/QA, optional Vosk ASR and recoverable stage-status reports. Automatic speaker/turn/event/semantic analysis is still pending. See [recording import instructions](docs/14-recording-import.md) and the [primary-workflow migration PR](https://github.com/lybym/AIVoiceBench/pull/28).

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for offline validation commands, [project context](docs/05-project-context.md) for the complete delivery agreement, [contract versions](docs/07-contract-versions.md) for migration and trigger semantics, and [work log](docs/06-work-log.md) for actual progress and untested dependencies.


### Web release v0.1.2

The browser workspace supports WAV, MP3 and M4A imports, persistent original and normalized evidence, consistent history/detail status, audio playback and Markdown report download. Docker includes FFmpeg. Health and OpenAPI read the same application version. Release publication checks the tag against that version and smoke-tests the built container with synthetic recordings in all three formats. Speaker attribution and semantic conclusions still abstain when evidence is unavailable; this is not real-device MVP acceptance.

### Model management v0.1.3

Use the browser Model Management page to register provider/model profiles and select defaults for result analysis or future speech capabilities. Existing compatible Chat Completions result analysis is wired; speech adapters are explicitly pending integration. Keys are write-only, configuration revisions prevent stale writes and each Web Run keeps a non-secret snapshot. See [model management](docs/16-model-management.md) for persistence, credential handling and deployment scope.
