# Speaker clustering / Attribution / Fusion / Events

2026-09-11 基线 main c612d36 / alpha.2；产品要求见 [PRD-F006～F009](PRD.md)。

ImportRun 分别登记 acoustic、diarization、attribution、fusion、turns、timeline、metrics。ASRNativeDiarizationProvider 复用已有 ASR 原生响应/Transcript 标签，不追加服务调用；需配置 ASR 与 diarization 路由。缺标签则 insufficient_evidence，不按轮流发言补标签。

聚类与角色分开：speaker ID 按录音 Hash 隔离并保留 invocation/原生响应引用；不同录音的 speaker_0 不是同一身份。聚类 confidence 留空，角色默认 unknown。内部 attribute_speakers() 接受显式映射，普通 Web/CLI 没有角色编辑入口。[PR #54](https://github.com/lybym/AIVoiceBench/pull/54) 的语义角色处理器尚未合并。

跨聚类声学片段按边界拆分，重叠标签冲突以 ambiguous_overlap 弃权，不整段归给最大重叠者。拆分文本来自相应 ASR 区间，不把全文复制到每段；原始声学边界与 provider_utterance_estimate 分开记录，不把估计边界变成声学真值。

有已知角色才构建 turns/responses 和 Timeline/指标；_timeline() 绑定 Run 身份。未知角色下相关阶段不足证据。完整旧/新回答语义关联、timeout 健康窗口和真实混音打断判定仍待验收。

依据：[diarization.py](../aivoicebench/diarization.py)、[fusion.py](../aivoicebench/fusion.py)、[import_pipeline.py](../aivoicebench/import_pipeline.py)；测试：[ASR 聚类](../tests/test_asr_diarization.py)、[融合](../tests/test_fusion_speakers.py)、[显式角色端到端](../tests/test_explicit_attribution_e2e.py)。注入响应/人工角色 fixture 不证明实际服务标签契约、角色识别或真实设备准确性。

独立 fusion CLI 保留；用法为 python -m aivoicebench fusion acoustic.json --output artifacts/fusion。历史算法见 [快照](product/archive/2026-09-10/18-fusion-turns-events.md)。
