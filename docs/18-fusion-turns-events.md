# Speaker clustering / Attribution / Fusion / Events

2026-09-11 基线 main c612d36 / alpha.2；产品要求见 [PRD-F006～F009](PRD.md)。

ImportRun 分别登记 acoustic、diarization、attribution、fusion、turns、timeline、metrics。ASRNativeDiarizationProvider 复用已有 ASR 原生响应/Transcript 标签，不追加服务调用；需配置 ASR 与 diarization 路由。`partial` Transcript 中仍合法的 utterance/speaker labels 继续作为 evidence，gap 单独保留；缺标签则 insufficient_evidence，不按轮流发言补标签。

聚类与角色分开：speaker ID 按录音 Hash 隔离并保留 invocation/原生响应引用；不同录音的 speaker_0 不是同一身份。聚类 confidence 留空，角色默认 unknown。Recording Analysis 不调用 LLM 判断角色；用户在 Evidence Workbench 中人工标记 tester/device/unknown，并以新 AnalysisRevision 保存后才重跑下游。当前普通 Web/CLI 尚无角色编辑入口，由 [#95](https://github.com/lybym/AIVoiceBench/issues/95) 跟踪。

跨聚类声学片段按边界拆分，重叠标签冲突以 ambiguous_overlap 弃权，不整段归给最大重叠者。拆分文本来自相应 ASR 区间，不把全文复制到每段；原始声学边界与 provider_utterance_estimate 分开记录，不把估计边界变成声学真值。

acoustic segment ↔ ASR speaker span 的对齐由独立确定性模块 [alignment.py](../aivoicebench/alignment.py) 计算，fusion 只物化它的结论，因此重叠规则只有一份实现。每个 acoustic segment 得到一个显式状态：`unmatched | single_cluster | multi_cluster | conflict`；匹配项保留双方区间、交集、有符号边界偏移、两个方向的重叠比例与聚类身份。诊断输出 `unmatched_acoustic_ms`、`unmatched_speaker_ms`、逐聚类 `coverage_ratio`、`low_energy` 分布与 `boundary_drift_ms`，全部带分母。对齐文档以 `speaker-alignment` artifact 登记（`alignment.json`）并校验独立 schema；它不含 `speaker_role`，不把匿名 cluster 映射成 tester/device。

2026-09-18 的诊断样本出现“ASR 有 cluster、84 个 acoustic segments 均无 cluster/role”的对齐缺口；[#94](https://github.com/lybym/AIVoiceBench/issues/94) 已按上述策略实现，指标不再空白但仍然弃权时，API `metrics_gap`、Run API/Web 与报告会给出原因与计数（未匹配时长、冲突片段数、low-energy 覆盖、角色未确认），而不是只显示空表。

有已知角色才构建 turns/responses 和 Timeline/指标；_timeline() 绑定 Run 身份。未知角色下相关阶段不足证据，且 `metrics_gap` 会显式说明 `roles_not_confirmed`。完整旧/新回答语义关联、timeout 健康窗口和真实混音打断判定仍待验收。

Active Measurement 将通过独立 Online Event Producer 产生相同 Canonical Event 类型；它利用已知 Stimulus Reference 形成 tester evidence，而不是机械复用 `speaker_0/speaker_1` 角色映射。两条 Pipeline 的声学 Evidence ID/Artifact 必须不同，event producer/policy 进入 provenance；下游继续使用同一 Timeline/Metric 语义。单麦克风 overlap 的旧 response stop boundary 在 reference cancellation/AEC/source-aware evidence 就绪前保持 insufficient_evidence。

依据：[diarization.py](../aivoicebench/diarization.py)、[fusion.py](../aivoicebench/fusion.py)、[alignment.py](../aivoicebench/alignment.py)、[import_pipeline.py](../aivoicebench/import_pipeline.py)；测试：[ASR 聚类](../tests/test_asr_diarization.py)、[融合](../tests/test_fusion_speakers.py)、[对齐 fixtures](../tests/test_alignment.py)、[显式角色端到端](../tests/test_explicit_attribution_e2e.py)。注入响应/人工角色 fixture 不证明实际服务标签契约、角色识别或真实设备准确性；对齐覆盖率也只是测量事实，不证明 speaker coverage 质量。

独立 fusion CLI 保留；用法为 python -m aivoicebench fusion acoustic.json --output artifacts/fusion。历史算法见 [快照](product/archive/2026-09-10/18-fusion-turns-events.md)。
