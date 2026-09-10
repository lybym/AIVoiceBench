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

源码 CLI 导入说明见 [recording import](docs/14-recording-import.md)，协作与测试说明见 [CONTRIBUTING](CONTRIBUTING.md)，开发规则见 [AGENTS](AGENTS.md)。不提交密钥、用户真实录音或个人报告。
