# Audio station — hardware acceptance pending

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

The target is a voice-conversation terminal with built-in speaker/microphone. The user has not prepared a target or external station routing. This module is generic across terminal models. No actual playback, recording or loopback calibration has been performed in this task.

## Windows commands

Install optional dependencies and enumerate devices without recording:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-audio.txt
.\.venv\Scripts\python.exe -m aivoicebench audio devices
```

Indices change after reconnect/reboot; different host APIs may list the same hardware multiple times. Choose compatible input/output host APIs and verify 16 kHz support. Unsupported formats are rejected instead of silently resampled. This dependency is optional for contract/preparation use; full audio tests and CI install it.

The following commands actually play and record. Replace the illustrative indices with the verified station configuration, after physically connecting the target or loopback path:

```powershell
python -m aivoicebench audio capture path/to/frozen.wav --input-device 1 --output-device 4 --input-channels 2 --output-channels 2 --output artifacts/audio
python -m aivoicebench audio calibrate --input-device 1 --output-device 4 --repetitions 3 --output artifacts/audio
```

These are explicit station commands, separate from preparation-only `run` until its integration is validated. No default microphone is opened. Capture supports PCM16/16kHz mono source and mono/stereo input/output, configurable pre-roll/tail, and a 10-minute memory bound. Source hash is frozen at preparation.

## Evidence and timing

Capture writes capture.wav, digital stimulus-reference.wav, source/hash/format/duration metadata governed by audio-capture.schema.json, and driver-timing.json. Digital output reference is not an acoustic measurement. A room microphone captures stimulus/device/room mix; never silently relabel it isolated device output. Stereo permits externally separated routing, but channel-role verification and Run Evidence normalization remain pending.

Driver ADC/DAC/current timestamps stay in the PortAudio stream clock. Python monotonic start/end identify the command interval and are not substituted for sample timestamps. Driver times are estimates, not acoustic or device-internal event boundaries. Overflow/underflow, invalid/nonmonotonic timestamps, timeout or interruption makes capture partial with an explicit reason. Buffered samples are retained where possible. No calibration correction is automatically applied.

Calibration plays a fixed low-level pseudorandom probe, records repetitions and locates it by normalized correlation. Missing/weak match yields insufficient_evidence. Raw sample alignment delay and a separate driver-time path estimate retain source/capture hashes. Count/min/median/max and each trial are reported; an incomplete repetition set is not an accepted calibration. Correlation 0.8 is an initial detection setting, not a product release threshold. The result includes the configured physical/acoustic route and cannot identify internal DUT stage delay or ERLE. Inspect ambiguous echoes/noisy paths manually; software validation cannot certify a room setup.

## Validation and API sources

Software tests inject known delay, gain, polarity, noise, stream-open failure, callback exceptions and overflow flags. No test harness input is a real HIL recording. Physical reproducibility, clock accuracy, channel isolation and hardware stress remain untested.

Official API checked 2026-09-07: [Stream callbacks and clocks](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html), [device enumeration and format checks](https://python-sounddevice.readthedocs.io/en/latest/api/checking-hardware.html). The callback fills buffers, retains ADC/DAC/current time and does no disk writes. Fixed blocks and preallocated buffers bound the MVP; actual realtime behavior still needs hardware tests. Future HIL deployment must verify PortAudio/platform dependencies on its actual station host; Windows executable delivery is not required by the current PRD.
