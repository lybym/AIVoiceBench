# AIVoiceBench

面向 AI 语音终端、AI 玩具和语音智能体的可复现、可追溯、可比较、可回归评测 Harness。

## 两条独立正式测量链

```text
Active Measurement                    Recording Analysis
Live Measurement Audio                External Recording
Online Event Producer                 Offline Event Producer
                  \                   /
                   Canonical EventTimeline
                              ↓
                    Canonical Metric Engine
                              ↓
                         MetricResult
```

- **Active Measurement**：平台主动播放测试语音，通过本地 Measurement Capture 采集现场声学信号，并逐步形成正式事件和指标。
- **Recording Analysis**：导入手机、录音笔或另一台电脑产生的独立 External Recording，执行离线声学与语义分析。

两条 Pipeline 不共享同一份 Audio Evidence；它们共享 Canonical Event 语义、指标定义、MetricResult 契约和版本化 Measurement Policy。External Recording 可用于独立复测、深度分析和 Measurement Equivalence 验证，但不是 Active Measurement 结果“转正”的前置条件。

## 当前实现边界

本轮文档审计基于 main `0362b22`：

- Recording Analysis 已具备录音导入、标准化、部分声学/ASR/归属/融合，以及有足够角色证据时的 Timeline 和 canonical metrics；Judge、Findings、人工修订、完整报告与真实录音验收仍未闭环。
- Active Voice Test 已具备浏览器播放、Control RMS VAD、Streaming ASR、Fixed/Free 控制与 execution record 基础；跨整次 Active Run 的 durable Measurement Audio、Stimulus Alignment、Streaming Acoustic Measurement、Canonical Live Timeline 和正式 Active MetricResult 仍为 planned。
- 软件验证、浏览器验证、容器验证、真实设备验证和 Measurement Equivalence 验证是不同状态。当前没有 `measurement_equivalence_verified` 声明。

## 先读文档

- [中心 PRD：产品范围、验收与实现状态](docs/PRD.md)
- [Active Measurement 技术边界](docs/25-active-measurement.md)
- [总体架构](docs/01-system-architecture.md)
- [测试方法](docs/02-test-methodology.md)
- [指标定义](docs/03-metric-definition.md)
- [Development Roadmap](docs/04-development-roadmap.md)
- [完整文档导航](docs/README.md)

根目录 [AGENTS.md](AGENTS.md) 规定开发和状态声明规则。旧产品文件位于 `docs/product/archive/`，只作历史证据，不是当前要求。

## 交付形态

当前交付为 Docker 后端与前端，通过 Windows 浏览器访问；不要求 Windows EXE/安装包。Docker 内包含媒体处理依赖，运行数据通过持久卷保存。稳定版与预览版的具体启动命令、附件和 SHA-256 以 [Docker/API 文档](docs/20-docker-api.md) 与对应 GitHub Release 为准。

默认部署面向可信单用户 localhost。模型和语音服务凭据只由后端持有，不进入浏览器、Git、运行快照或报告；保存配置不等于服务连通或真实效果已经验证。

## 验证原则

合成 fixture 用于验证契约、状态机和确定性公式，不能证明实体设备表现。正式结果必须回溯到独立声学 Artifact、sample-indexed timebase、Measurement Policy、Evidence、confidence/uncertainty 和处理器版本；证据不足时输出 `insufficient_evidence`，不能猜测声学边界、说话人或设备内部根因。
