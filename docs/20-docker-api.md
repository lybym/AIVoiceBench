# Docker / API — 当前使用说明

2026-09-11 基线 main c612d36 / alpha.2。产品要求见 [PRD](PRD.md)，主链配置见 [Recording Backbone](23-recording-backbone.md)。

## 版本与启动

[v0.2.0-alpha.2](https://github.com/lybym/AIVoiceBench/releases/tag/v0.2.0-alpha.2) 已公开为预览版；Latest 稳定版仍为 v0.1.3。alpha.1 未作为公开预览版发布。按 [安装说明](releases/0.2.0-alpha.2.md) 下载镜像包、Compose、Windows 启动脚本及 SHA256SUMS，启动后访问 <http://127.0.0.1:8000>；端口占用可用脚本 -Port 8001。

预览脚本使用独立容器/数据卷，不迁移旧 v0.1.x 数据。Docker 自带 FFmpeg/FFprobe，无需从 Windows 挂载可执行文件；不交付 EXE。

## 接口与边界

| 方法 | 路径 | 当前用途 |
| --- | --- | --- |
| GET | /health、/openapi.json、/ | 版本/接口信息与 Web |
| POST | /api/analyze | multipart 三格式导入，与 CLI 共用 ImportRun；设备资料可选 |
| GET | /api/runs、/api/runs/{run_id} | 历史/详情：analysis_id、stages、transcript、speaker_segments、diarization_scope、attribution、阶段数据与 report_md |
| GET | /api/runs/{run_id}/audio | 标准化音频回放 |
| POST | /api/runs/{run_id}/resume | JSON {"retry_asr": true} 显式重试 ASR；已完成 ASR/report 不重复调用 |
| GET / POST | /api/models | 脱敏配置读取/版本化更新；保存不调用服务，密钥不回传 |

上传上限 1 GiB、最长 30 分钟为实现限制。云 ASR 需要路由、凭据及音频 PUT/GET/host 配置，不能只填 Key；ASR-native 聚类需绑定 diarization 路由且服务返回标签。普通 Web 无角色编辑入口，缺角色时 turns/timeline/metrics 弃权。

ImportRun 尚未执行 Judge/Findings，当前报告是阶段状态报告，空发现不代表设备通过。完整结论报告、人工修订和通用重分析 API/UI 未闭环；resume 不是通用重算接口。

默认可信单用户 localhost 部署，配置数据库为本地明文，远程访问需认证代理。音频发布 URL 必须为允许主机的 HTTPS、443（或省略端口），PUT/GET 同对象并回读校验；模型管理对本地服务 HTTP 的许可不适用于音频发布。

依据：[api.py](../aivoicebench/api.py)、[import_pipeline.py](../aivoicebench/import_pipeline.py)、[cloud_transport.py](../aivoicebench/cloud_transport.py)。软件/容器验证见 PRD 第 1 节；真实录音质量验收未完成。
