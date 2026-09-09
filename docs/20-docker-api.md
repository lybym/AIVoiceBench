# AIVoiceBench Docker — Recording Import & Analysis API

打包已完成的分析管线为可运行的 Docker 容器。

## 快速启动

```bash
# 1. 创建数据目录
mkdir -p data/recordings data/output data/cache

# 2. 构建并启动
docker-compose up

# 3. 访问 API 文档
#    http://localhost:8000/docs

# 4. 分析一段录音 (WAV)
curl -X POST http://localhost:8000/api/analyze \
  -F "file=@data/recordings/conversation.wav" \
  -F "device=我的AI设备" \
  -F "supplier=供应商A"

# 5. 查看历史分析
curl http://localhost:8000/api/runs
```

## 支持的输入

| 格式 | 状态 | 说明 |
| --- | --- | --- |
| WAV (任意采样率/声道) | ✅ 内置转换 | Python stdlib 转换为 PCM16 16kHz mono |
| WAV (PCM16 16kHz mono) | ✅ 直接处理 | 无需转换 |
| MP3 / M4A | ⚠️ 需 FFmpeg | 挂载 FFmpeg 或预转换 |

### MP3/M4A 支持（可选）

取消 `docker-compose.yml` 中 FFmpeg 挂载的注释：

```yaml
volumes:
  - /usr/bin/ffmpeg:/usr/local/bin/ffmpeg:ro
  - /usr/bin/ffprobe:/usr/local/bin/ffprobe:ro
```

或在容器内安装：

```bash
docker exec -it aivoicebench apt-get update && apt-get install -y ffmpeg
```

## API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/docs` | Swagger UI 交互文档 |
| POST | `/api/analyze` | 上传 WAV，运行完整分析管线 |
| GET | `/api/runs` | 列出历史分析 |
| GET | `/api/runs/{run_id}` | 获取单次分析详情 |

### POST /api/analyze 参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| file | File | 必填 | WAV 录音文件 |
| device | string | null | 设备名称 |
| hardware | string | null | 硬件版本 |
| firmware | string | null | 固件版本 |
| model | string | null | AI 模型 |
| prompt | string | null | Prompt 版本 |
| supplier | string | null | 供应商 |
| environment | string | null | 测试环境 |
| notes | string | null | 备注 |
| frame_ms | float | 30 | 声学分析帧长 |
| hop_ms | float | 10 | 帧移（时间不确定度） |
| min_speech_ms | float | 100 | 最小语音段 |
| min_silence_ms | float | 200 | 最小静音段 |
| timeout_ms | float | 5000 | 超时阈值 |
| false_endpoint_ms | float | 300 | 假端点检测阈值 |

## 分析管线

```
上传 WAV
  → Python stdlib 标准化 (PCM16 16kHz mono)
  → 能量 VAD 声学分割 (#23)
  → 段融合 + 说话人归因 (#24)
  → 轮次构建 (#24)
  → 事件检测 (#24)
  → EventTimeline 生成 (#24)
  → 时延指标计算 (#25)
  → JSON 结果返回 + 持久化到 /data/output
```

## 数据目录结构

```
data/
  recordings/     # 输入录音 (只读挂载)
  output/         # 分析结果 (持久化)
    RUN-xxxx/
      source.wav
      normalized.wav
      acoustic-segments.json
      fused-segments.json
      turns.json
      timeline.json
      metrics.json
      profile.json
  cache/          # 可选: Vosk 模型等
```

## 配置外挂

通过环境变量或挂载配置文件：

```yaml
# docker-compose.yml
environment:
  - AIVOICEBENCH_OUTPUT=/data/output
  - AIVOICEBENCH_CACHE=/data/cache
volumes:
  - ./config/aivoicebench.yaml:/app/config/aivoicebench.yaml:ro
```

## 当前限制

1. **无 LLM 评估** — Feedback Latency 和 Meaningful Response Latency 返回 `insufficient_evidence`
2. **说话人归因为启发式** — 交替假设 (confidence 0.5)，需人工或 diarization 验证
3. **无 Finding 生成** — 仅输出指标和事件，不生成质量缺陷判定
4. **无报告渲染** — 输出 JSON，不生成 Markdown 报告
5. **无 UI** — 仅有 API 端点，无前端页面

## 下一步

- 集成 #21 录音导入管线（FFmpeg 标准化、artifact 管理、部分 Run 恢复）
- 添加 LLM Harness (#10) 计算 Feedback/Meaningful Response Latency
- 添加 Finding 生成和 Markdown 报告
- 添加 Web UI (Home/Import/Analysis/Metrics/Findings)
