# Audio Station — optional professional HIL path

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

2026-09-16 架构决定：**Linux Server + Docker + Remote Chrome Browser Station 是正式主架构。** 本文描述的原生 Audio Station 只保留为未来专业 HIL / Measurement Equivalence / low-level audio 扩展，不再被理解为 Active Measurement 的默认宿主机，也不要求把 AIVoiceBench Server 迁移到 Windows。

## 1. Positioning

默认路径：

```text
Remote Chrome Browser Station
        ↕ HTTPS / WebSocket
Linux Server + Docker
```

可选专业路径：

```text
Windows / Linux / macOS Audio Station Agent
├─ dedicated audio device
├─ PortAudio / future WASAPI-specific adapter
├─ loopback / multi-channel / calibration
└─ local sample clock
        ↕ station protocol
Linux AIVoiceBench Server
```

Browser Measurement Capture 使用自己的 contract，因为浏览器不能如实提供 PortAudio driver/device/duplex 字段。两条路径最终必须输出兼容 Canonical Event 语义并使用同一 Metric Engine。

Native Windows 只有在需要 WASAPI loopback、driver-level timing、USB/串口、专用多通道声卡等能力时才值得增加；它不是为了降低普通对话 RTT，也不是当前基础交付要求。

## 2. Existing optional station commands

安装可选依赖并枚举设备：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-audio.txt
.\.venv\Scripts\python.exe -m aivoicebench audio devices
```

显式播放/录制与 calibration：

```powershell
python -m aivoicebench audio capture path/to/frozen.wav --input-device 1 --output-device 4 --input-channels 2 --output-channels 2 --output artifacts/audio
python -m aivoicebench audio calibrate --input-device 1 --output-device 4 --repetitions 3 --output artifacts/audio
```

这些命令属于可选 Station path；没有经过真实 HIL 验证时不得用于宣称专业音频测量已完成。

## 3. Evidence and timing

Station capture 保存 `capture.wav`、digital stimulus reference、source/hash/format/duration metadata 和 driver timing。Digital output reference 不是 acoustic measurement。Room microphone 捕获的是 stimulus/device/room mix；不能静默标成 isolated device output。

Driver ADC/DAC/current timestamps 保持在各自 driver/stream clock 中。Python monotonic start/end 只标识命令区间。任何 driver time 都不能未经 calibration/mapping 直接替代 acoustic sample timeline。

Browser Station 的 Active Measurement 采用 [25-active-measurement.md](25-active-measurement.md) 定义的 local sample counter；Audio Station 若未来形成正式 Measurement Producer，也必须输出 sample-indexed audio timebase、capture integrity 和 clock mapping provenance。

## 4. Calibration

Calibration 可以播放固定 probe 并通过 correlation 找到录音中的匹配位置。Missing/weak match → `insufficient_evidence`。原始 sample alignment delay、driver-time path estimate、source/capture hashes 都应保留。

校准结果不能反推出 DUT 内部 ASR/LLM/TTS stage delay，也不能因为 loopback 成功就自动证明房间声学路径、AEC 或 ERLE。

## 5. Relationship to Browser Station

Browser Station 是近期默认：

- `getUserMedia` / `AudioWorklet`；
- Chrome 本地音频 I/O；
- TEN VAD target / RMS fallback；
- local sample counter；
- Web UI；
- 向 Linux Server 传输 PCM 与 control messages。

Audio Station 是将来需要更低层音频控制时增加的 adapter，而不是替换 Browser Station 与 Server 架构。两者可以共存，并用于 Measurement Equivalence。

## 6. Validation boundary

Software tests 可以注入 delay、gain、polarity、noise、stream-open failure、callback exception 和 overflow flag；这不等于真实 HIL。

Physical reproducibility、clock accuracy、channel isolation、loopback routing、SPL、driver behavior 和 hardware stress 必须在实际 Station 上另行验证。

当前 PRD 不要求 Windows executable delivery。