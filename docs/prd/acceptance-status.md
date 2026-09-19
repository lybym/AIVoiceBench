# 验收与状态

本文承载 PRD-N001–N007、M1 验收门槛、状态语言和正式里程碑门槛。状态是审计事实，不会降低任何产品需求。

## 状态语言

| 代码状态 | 含义 |
| --- | --- |
| ✅ `implemented` | 有限范围的实现及针对性验证存在；不等于真实验收完成 |
| 🟡 `partial` | 已有基础或局部能力，必要闭环仍缺失 |
| ⬜ `planned` | 无可交付实现；设计、配置或关闭 Issue 不算实现 |
| ⏸ `deferred` | 不在当前 MVP 排程，保留已有基础 |

验证状态独立：`software_verified`、`container_verified`、`browser_verified`、`real_cloud_verified`、`real_recording_pending`/`real_recording_verified`、`real_device_pending`、`validation_pending`。除非有直接证据，不能声明 `real_recording_verified` 或 `measurement_equivalence_verified`。状态语言由 `AcceptanceRecord 1.0.0` 逐 gate 落盘（见下），未达到的 gate 记 `pending`/`not_reached`，不得省略。

## 非功能要求

| ID | 要求 |
| --- | --- |
| PRD-N001 | Evidence First：结论回溯 Run → Turn → Event → 音频区间 → Evidence → 版本；不足时弃权 |
| PRD-N002 | 原件、机器输出、人工修订分层且不可静默覆盖；Hash 和 provenance 可复核 |
| PRD-N003 | 时间、公式、统计与验证由确定性代码执行；语义节点受约束且引用证据 |
| PRD-N004 | 凭据只由后端持有；浏览器、Git、快照、报告不出现长期密钥 |
| PRD-N005 | 设备内部根因未获设备日志时不得伪称已测得；外部 ASR 不等于设备内部 ASR |
| PRD-N006 | 需求、Issue、分支、PR、测试和工作日志可追溯；合并须有明确授权 |
| PRD-N007 | 正式测量记录独立 Artifact、sample-index timebase、policy/processor/version、完整性、confidence/uncertainty 与最终化状态；跨 Pipeline 比较声明可比性 |

## M1 录音分析验收门槛

M1 尚未通过。最终验收需要授权的真实 5–20 分钟录音、有效云服务调用、人工标注/复核与 Windows 浏览器完整路径。

- 导入、标准化、QA、File ASR、聚类/归属、Timeline、MetricResult、语义、Findings、人工修订和报告的实际链路必须分别呈现成功、失败、缺失和弃权。
- 软件 fixture、预览版或 CI 只能证明有限契约，不能代替真实录音质量、真实云服务、实体设备或人工标注。
- 验收记录必须保留 Artifact/Hash、版本、环境、样本选择、分母、失败样本和不确定性。

### 验收证据契约（不改变门槛）

M1 的五个 gate（`software_verified`、`container_verified`、`browser_verified`、`real_cloud_verified`、`real_recording_verified`）由 `AcceptanceRecord 1.0.0`（`schemas/acceptance-evidence.schema.json`）记录，并由确定性检查器 `aivoicebench/acceptance_evidence.py`（CLI `python -m aivoicebench acceptance init|check`）校验。该契约把本节门槛变成可执行检查，**不降低任何门槛**。产生绿色结论的调用必须显式启用两项控制：

```text
python -m aivoicebench acceptance check <record.json> \
  --verify-artifacts --repository-root <repo>
```

- `real_recording_verified` 只在存在授权真实样本（5–20 分钟区间）、人工复核证据与机器原件分离、**声明的 artifact 与 evidence 摘要经 `--verify-artifacts` 实际重算匹配**、且 **`--repository-root` 指向的仓库目录被真实遍历且未发现 Git 内音频**时才成立；synthetic/fixture 样本、mock、CI、机器生成却被记为人工的标注会被判为未授权声明并使记录 `invalid`。
- **区间本身是声明值核对，不是测量值。** 5–20 分钟区间由样本自报的 `duration_ms` 判定；检查器不解码音频，因此该条件只与记录自身对账，结果中由 `declared_only_controls` 明确列出（Markdown 报告为 “Conditions checked against declared values”）。同一列表中还包括 `source`/`device`/`authorization` 等只能被拒绝、无法被证实的声明。
- **声明的 artifact 尺寸必须与保留文件对账。** 样本 artifact 声明的 `byte_length` 与实际文件字节数不一致时判为 error；授权真实样本的 audio artifact 未声明 `byte_length` 时为 blocking gap。artifact 摘要不能同时让尺寸自由声明。
- **每个授权样本必须绑定各自的 artifact。** 两个样本声明同一 `sha256`（同一物理文件被计为两条录音）判为 error；同一样本内重复声明同一摘要同样判为 error。授权样本数是被报告的结果，不得被重复引用放大。
- **真实录音 gate 的 `human_review` 证据必须是记录自己声明的人工复核 artifact。** 仅按 `kind` 与 `sample_id` 绑定不够：该证据的 `sha256` 必须等于 `human_reviews[]` 中某条已声明复核 artifact 的摘要，否则判为未授权声明（记录 `invalid`）。否则任意一个本地可达、摘要自洽的文件都能承载整条真实录音声明，而 `human_reviews[]`（PRD-N002 要求声明所依托的人工层）却指向别处。比对同时覆盖入口是否有摘要：缺摘要即无法证明它属于人工层。
- **人工复核层同样适用摘要唯一性。** 同一条保留的标注 artifact 被两条不同 `review_id` 的复核复用判为 error（#85 要求人工复核覆盖多个不同事项），结果同时报告 `human_review_count` 使人工层规模可审计。
- 未请求 artifact/evidence 校验、未提供仓库根、仓库根**不存在或不是目录**、或记录声明的 `exposure_scan.scan_roots` 条目无法遍历时，该 gate 不予授权，CLI 也不会以 0 退出。**未被执行的对照不得被报告为已执行**。
- 授权真实 gate 的 evidence 与每个 stage 分母的 evidence 都必须解析到本地真实存在的 Artifact 且摘要匹配（声明了摘要却不匹配为 error，无法解析为 blocking gap）；只有 `--verify-artifacts` 会执行该解析。
- 每个 stage 的分母必须显式计入 complete/partial/failed/unknown/abstained/not_applicable 且与 `expected_total` 对账，**禁止只报成功的分母**；**任何**已声明的 evaluation（含 `failed`/`abstained`/`not_attempted`）都必须有其 stage 的分母。
- 声明真实录音 gate 的记录必须覆盖 M1 的全部 stage（`not_applicable` 是有效答案）；某 stage 整体缺席时，其失败/未知/弃权无法计入分母，故视为未授权。
- 每条结论必须可回溯 Run → Turn/Event → 音频区间 → Evidence → processor/model/policy version；证据 id 必须在记录内唯一声明，缺口作为显式 blocking gap 报出。
- 真实云 gate 的 evidence 必须以显式 `sample_id` 绑定到**授权真实样本**（路径子串不算绑定），synthetic/fixture 样本不得授权真实云 gate。
- 角色修改必须生成新 AnalysisRevision、保留旧结果字节并给出 recompute/diff 证据，且不得重跑识别/聚类。
- 未达到的 gate 必须写明缺口；**没有真实验收记录时不得把任何 gate 写成已验证**。blocking gap 未清零时记录不得判为 `complete`。
- 判为 `verified` 的 gate 必须带 `verified_at` 时间戳；不能定位在时间轴上的验证不可审计。
- 暴露扫描读取被遍历根下**每个可解码为文本的文件**，无法解码的文件作为未覆盖项报出而不是当作干净；同一个根被重复声明时只遍历一次。扫描**不做降级遍历**的目录（`dist`/`build`/`node_modules`/`.git`/`.venv`/`__pycache__`/缓存目录等环境与构建产物）逐条记入 `repository_directories_skipped`，其内容**不在本次扫描覆盖范围内**；二进制后缀、超过 8 MiB 的文件也分别记入 `repository_files_unread`。上述排除项与“定义/验证检测模式本身的策略源码”是扫描的**全部**排除项，且全部出现在结果与 Markdown 报告中——"clean" 不得表示"没有看过"。
- **声明的时长/采样率/声道/编码与声明的 artifact 尺寸必须自洽。** 对未被压缩的 raw PCM 编码（`PCM_S16LE` 等），记录自身声明的 `duration_ms × sample_rate_hz × channels × 每样本字节数` 即为 artifact 应有的大小；与 `byte_length` 不符判为 error（1 ms 容差）。对 mp3/m4a/opus 等压缩或容器格式**不发明**任何尺寸算术，只做文件尺寸对账。**这道算术是否行使必须被具名披露**：`encoding` 是自由文本，未识别拼写（如 `wav`）或压缩编码会让该对照不适用，因此结果给出 `audio_size_arithmetic_applied` 与 `audio_size_arithmetic_not_applicable`，Markdown 报告列出 “Audio size arithmetic NOT applied to …”。控制未行使时**不得**读作已行使——这是 `repository_directories_skipped`/`repository_files_unread`/`detection_policy_sources_skipped` 同一原则在此处的落实。

检查器本身只是工具，其通过不等于 M1 通过；它只能证明声明摘要与本地文件一致、声明之间互相授权，**不能证明某个声明样本确实是授权真实录音**。区间、来源与授权等只能与记录自身对账的条件已在结果 `declared_only_controls` 中逐项列出，不得据此声称检查器测量过录音。M1 仍以真实证据记录为准。

## 正式里程碑

| 里程碑 | 目标 | 通过门槛 |
| --- | --- | --- |
| M1 | Recording Analysis MVP | 上述真实录音与人工复核门槛通过 |
| M2 | Fixed Active Voice Test | 冻结 Case/刺激、可靠控制、Live Measurement Audio 与正式 Active Result 最小闭环 |
| M3 | Free Test Agent | 受控 Agent、实时 Control、预算/Coverage、完整 Trace 与真实设备验收 |
| M4 | 独立关联与 Equivalence | 可复核配对、时钟/不确定性记录与已批准的偏差/一致性分析 |
| M5 | Compare/Regression | 经复核问题可最小化为固定 Case，并输出可比差异证据 |

M2–M5 不会降低 M1 的真实录音门槛；控制成功、播放完成或 Agent 运行完成均不自动代表正式 Measurement Result 通过。
