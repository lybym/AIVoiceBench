# Docker / API — 使用说明与版本边界

> M1 实现更新：本分支统一 ImportRun、ASR 路由、调用审计和转写；恢复/契约与当前验证限制见 [Recording Backbone](23-recording-backbone.md)。下文旧版本路径和可用状态以该技术更新为准。

产品要求见 [PRD-F001/F014/F015、PRD-N001](PRD.md)，不以本文件另设交付范围。

## 选择运行的版本

main 19d3a07 尚未包含 PR #43/#45 的全部 Web 修复。发布版 v0.1.3 已包含 FFmpeg、Web WAV/MP3/M4A 导入、统一历史状态、模型管理和版本号修复。发布镜像与 main 源码不能混为同一实现。

推荐从 [v0.1.3 Release](https://github.com/lybym/AIVoiceBench/releases/tag/v0.1.3) 下载并按 [README](../README.md) 运行，浏览器访问 http://localhost:8000 。升级时保留数据卷。无需把 FFmpeg 可执行文件从 Windows 挂入 Linux 容器。

## 发布版接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /health、/openapi.json | 服务/版本元数据 |
| GET | / | Web UI |
| POST | /api/analyze | multipart 三格式上传；设备资料可选 |
| GET | /api/runs、/api/runs/{run_id} | 历史与统一响应详情 |
| GET | /api/runs/{run_id}/audio | 标准化音频回放 |
| GET / POST | /api/models | 脱敏设置读取、版本化配置更新；不回传密钥 |

上传上限 1 GiB、最长 30 分钟；完整 MVP 的真实录音验收仍是 PRD 第 7 节。失败阶段保留 Run，不把 partial 视为通过。模型管理和路径参数以 [发布代码](https://github.com/lybym/AIVoiceBench/blob/v0.1.3/aivoicebench/api.py) 为准；语音配置尚未代表云适配器实际调用。

API 是可信单用户服务。发布版 Compose 默认绑定 localhost；本地配置密钥数据库是明文存储，远程使用需认证代理。容器基础检查只验证软件行为，不验证设备准确率。

原先“WAV-only / 挂载 FFmpeg”的说明保留在 [历史快照](product/archive/2026-09-10/20-docker-api.md)，不再作为发布版操作指南。
