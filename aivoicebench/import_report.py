"""Revision-scoped import/analysis report.

One Run can hold several AnalysisRevisions. Each revision owns its own analysis
directory, so this writer produces the ``report.json``/``report.md`` **of exactly
one revision** and never touches an earlier one. That is what makes a human
revision auditable next to the machine original.

The report has to answer, without the reader opening artifacts:

- which revision and which manual role decision it was produced from;
- whether it is a provisional/diagnostic view or a role-confirmed one;
- which evidence is deterministic, which is semantic (provider recognition and the
  structured Judge) and which is human-reviewed;
- where every published region comes from, with its audio-relative interval and
  artifact reference;
- which stages failed, abstained or never ran, and why a metric list is empty.

Facts come from the persisted documents of this revision only. The evidence index
is the same projection the browser Evidence Workbench draws
(:mod:`aivoicebench.workbench`), so the page and the report cannot disagree about
geometry or about which values were observed.
"""

from html import escape
import json
from urllib.parse import quote

from .runner import write_json

#: Evidence class per registered artifact kind. The class names are the report's
#: own vocabulary and are documented in the rendered Markdown, so a reader never
#: has to guess which producer a document came from.
EVIDENCE_CLASSES = {
    'deterministic': (
        'original_recording', 'normalized_audio', 'audio_metadata', 'audio-qa',
        'audio_qa_conditions', 'acoustic-segments', 'speaker-assignments',
        'speaker-alignment', 'attribution', 'fused-segments', 'turns', 'timeline',
        'metrics',
    ),
    'semantic': (
        'transcript', 'asr_native', 'provider_invocation', 'judge-results',
        'judge-raw', 'findings',
    ),
    'human_reviewed': (
        'speaker-role-mapping',
    ),
}

EVIDENCE_CLASS_NOTES = {
    'deterministic': '确定性引擎证据：声学边界、聚类、对齐、Turn/EventTimeline、Canonical MetricResult 与音频原件。',
    'semantic': '语义证据：Provider 识别文本、Structured Judge 结果与由 Judge 派生的 Findings；不是声学测量真值。',
    'human_reviewed': '人工复核证据：逐聚类的用户角色决定所生成的不可变 revision；LLM 不参与角色归因。',
    'other': '派生复核面（role-review 状态面）、运行簿记与配置快照；不属于上述三类证据。',
}

_CLASS_OF = {kind: name for name, kinds in EVIDENCE_CLASSES.items() for kind in kinds}


def _text(value):
    return escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ').replace('`', '&#96;')


def _fmt_ms(value):
    if value is None:
        return 'N/A'
    return f'{float(value) / 1000:.2f}s'


def _fmt_range(start, end):
    return f'{_fmt_ms(start)} – {_fmt_ms(end)}'


def _fmt_confidence(value):
    """Render a confidence, or an explicit unknown marker.

    A derived interval legitimately carries no confidence number; ``0.00`` would
    present "no defensible number" as a measured zero.
    """
    if value is None:
        return '—'
    return f'{float(value):.2f}'


def _load(analysis, name):
    path = analysis / name
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_checked(analysis, name):
    """Return ``(payload, status)`` where status is ``ok``/``missing``/``unreadable``.

    A document that exists but cannot be parsed is not the same evidence as a
    document the pipeline never wrote, and a report must not present the first as
    the second.
    """
    path = analysis / name
    if not path.is_file():
        return {}, 'missing'
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}, 'unreadable'
    if not isinstance(payload, dict):
        return {}, 'unreadable'
    return payload, 'ok'


def _data(document, analysis):
    """Payload of a stage envelope, or a separately published document as-is."""
    if not document:
        return {}
    if 'kind' in document:
        payload = document.get('data')
        return payload if isinstance(payload, dict) else {}
    return document


def _alignment_section(run):
    """Explain why role-dependent metrics are absent, using the recorded evidence.

    A blank metric table must be traceable to a count of unmatched acoustic
    duration, an unmatched speaker duration or an unconfirmed role — never left to
    look like the report simply omitted data.
    """
    analysis = run.analysis
    alignment = _load(analysis, 'alignment.json')
    fused = _data(_load(analysis, 'fused-segments.json'), analysis)
    metrics = _data(_load(analysis, 'metrics.json'), analysis)
    timeline = _data(_load(analysis, 'timeline.json'), analysis)

    from .alignment import explain_metric_gap
    gap = explain_metric_gap(fused, timeline, metrics, alignment)
    lines = ['', '## 指标可用性', '']
    if gap['status'] == 'observed':
        lines.append(f'本次分析产生 {gap["observed_metric_count"]} 个已观测指标。')
        return lines
    lines.append('本次没有已观测指标。原因与计数如下，均可回溯到证据文件，不是报告漏字段：')
    lines.append('')
    lines.append('| 原因 | 涉及片段数 | 说明 |')
    lines.append('| --- | --- | --- |')
    for reason in gap['reasons']:
        lines.append(f'| {_text(reason["code"])} | {reason["count"]} | {_text(reason["detail"])} |')
    if alignment:
        diagnostics = alignment.get('diagnostics', {})
        lines += ['', '对齐诊断（acoustic segment ↔ ASR speaker span）：', '',
                  '| 指标 | 值 |', '| --- | --- |',
                  f'| acoustic 片段数 | {diagnostics.get("acoustic_segment_count")} |',
                  f'| speaker span 数 | {diagnostics.get("speaker_span_count")} |',
                  f'| 未匹配 acoustic 时长 (ms) | {diagnostics.get("unmatched_acoustic_ms")} |',
                  f'| 未匹配 speaker 时长 (ms) | {diagnostics.get("unmatched_speaker_ms")} |',
                  f'| 冲突片段数 | {len(diagnostics.get("conflicted_acoustic_segment_ids") or [])} |']
        low = diagnostics.get('low_energy') or {}
        if low:
            lines.append(f'| 低能量片段数 / 未匹配 (ms) | {low.get("segment_count")} / {low.get("unmatched_ms")} |')
        lines += ['', '逐聚类覆盖：', '', '| 聚类 | 原生标签 | speaker 时长 (ms) | 已覆盖 (ms) | 覆盖率 |',
                  '| --- | --- | --- | --- | --- |']
        for cluster in diagnostics.get('per_cluster') or []:
            lines.append(f'| {_text(cluster["speaker_id"])} | {_text(cluster.get("native_speaker_id"))} | '
                         f'{cluster["speaker_speech_ms"]} | {cluster["matched_ms"]} | {cluster.get("coverage_ratio")} |')
    return lines


def _classify(artifacts):
    """Bucket this revision's registered artifacts into evidence classes."""
    classes = {name: [] for name in EVIDENCE_CLASSES}
    classes['other'] = []
    for item in artifacts:
        entry = {'artifact_id': item.get('artifact_id'), 'kind': item.get('kind'),
                 'path': item.get('path'), 'sha256': item.get('sha256'),
                 'processor': item.get('processor')}
        classes[_CLASS_OF.get(item.get('kind'), 'other')].append(entry)
    return classes


def _revision_section(workbench):
    """Identity of the revision this report belongs to, and its review history."""
    gate = workbench.get('gate') or {}
    revision = workbench.get('revision')
    lines = ['', '## 报告范围与修订', '',
             f'- **AnalysisRevision**：`{_text(workbench.get("analysis_id"))}`',
             f'- **视图类型**：{"role_confirmed（已有人工角色确认）" if gate.get("role_dependent_available") else "provisional（角色确认未完成，仅为导入/诊断视图）"}',
             f'- **角色 Gate**：`{_text(gate.get("status"))}` — {_text(gate.get("reason"))}',
             f'- **证据文档**：`{_text(workbench.get("document_id"))}`']
    if revision:
        lines += [f'- **人工角色 revision**：`{_text(revision.get("revision_id"))}` '
                  f'(index {revision.get("revision_index")}, reviewer `{_text(revision.get("reviewer"))}`, '
                  f'{_text(revision.get("created_at"))})',
                  f'- **上一条 revision**：`{_text(revision.get("previous_revision_ref"))}`'
                  if revision.get('previous_revision_ref') else '- **上一条 revision**：无（首次人工确认）',
                  f'- **mapping SHA256**：`{_text(revision.get("mapping_sha256"))}`',
                  '- **机器原件与人工修订分离**：本报告只描述上述 revision；'
                  '更早 revision 的报告与产物保持原样，不被覆盖。']
    else:
        lines.append('- **人工角色 revision**：无。本报告不包含任何依赖 tester/device 角色的结论。')
    return lines


def _evidence_class_section(classes):
    lines = ['', '## 证据分级', '']
    for name in ('deterministic', 'semantic', 'human_reviewed'):
        entries = classes.get(name) or []
        lines += [f'### {name}（{len(entries)}）', '', _text(EVIDENCE_CLASS_NOTES[name]), '']
        if not entries:
            lines.append('本次 revision 没有该类证据。')
            lines.append('')
            continue
        lines += ['| Artifact | Kind | Processor | SHA256 |', '| --- | --- | --- | --- |']
        for entry in entries:
            lines.append(f'| {_text(entry["artifact_id"])} | {_text(entry["kind"])} | '
                         f'{_text(entry["processor"])} | {_text(entry["sha256"])} |')
        lines.append('')
    other = classes.get('other') or []
    if other:
        lines += [f'### other（{len(other)}）', '', _text(EVIDENCE_CLASS_NOTES['other']), '',
                  '| Artifact | Kind | Processor |', '| --- | --- | --- |']
        lines += [f'| {_text(entry["artifact_id"])} | {_text(entry["kind"])} | {_text(entry["processor"])} |'
                  for entry in other]
        lines.append('')
    return lines


def _metrics_section(workbench):
    """Observed metric values, verbatim from the Canonical Metric Engine.

    A value that was not observed is rendered as an explicit abstention with its
    recorded reason; the report never fills the cell with 0 or with a success
    placeholder.
    """
    metrics = workbench.get('metrics') or []
    lines = ['', '## 指标结果', '']
    if not metrics:
        lines += ['本次 revision 没有 MetricResult。原因见“指标可用性”。', '']
        return lines
    lines += ['| 指标 | 值 | 单位 | 状态 | 轮次 | 证据 Region | 弃权/失败原因 |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for metric in metrics:
        value = metric.get('value')
        rendered = '未观测（abstained）' if value is None else str(value)
        lines.append(f'| {_text(metric.get("name") or metric.get("metric_id"))} | {_text(rendered)} | '
                     f'{_text(metric.get("unit"))} | {_text(metric.get("status"))} | '
                     f'{_text(metric.get("turn_id") or "—")} | {_text(metric.get("region_id") or "—")} | '
                     f'{_text(metric.get("reason") or "—")} |')
    lines.append('')
    return lines


def _evidence_index_section(workbench):
    regions = workbench.get('regions') or []
    lines = ['', '## 证据索引', '',
             '每一行都是一个可点击到音频区间（`audio_relative_ms`）的证据 Region；坐标来自 `workbench` 投影，'
             '不在报告或浏览器中重新推导。', '']
    if not regions:
        lines += ['本次 revision 没有可定位的证据 Region。', '']
        return lines
    lines += ['| Region | 类型 | 音频区间 | 角色 | 角色依据 | 置信度 | 来源文档 | 证据引用 |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for region in regions:
        source = region.get('source') or {}
        refs = ', '.join((source.get('evidence_ids') or []) + (source.get('event_ids') or [])
                         + (source.get('metric_ids') or []) + (source.get('turn_ids') or []))
        lines.append(f'| {_text(region.get("region_id"))} | {_text(region.get("kind"))} | '
                     f'{_fmt_range(region.get("start_ms"), region.get("end_ms"))} | '
                     f'{_text(region.get("role") or "—")} | {_text(region.get("role_basis") or "—")} | '
                     f'{_fmt_confidence(region.get("confidence"))} | {_text(source.get("document"))} | '
                     f'{_text(refs)} |')
    lines.append('')
    return lines


def _availability_section(workbench, payload=None):
    unavailable = workbench.get('unavailable') or []
    abstentions = workbench.get('abstentions') or {}
    integrity = ((payload or {}).get('evidence_integrity')
                 or workbench.get('evidence_integrity') or {})
    lines = ['', '## 未完成、失败与弃权', '']
    unreadable = integrity.get('unreadable_documents') or []
    if unreadable:
        lines += [f'**证据链不完整：** {", ".join(_text(name) for name in unreadable)} '
                  '存在但无法解析；这些证据从本次投影中缺失，报告不把它们当作“空证据”。', '']
    if not unavailable:
        lines += ['本次 revision 的每个阶段都已 complete。', '']
    else:
        lines += ['| 阶段 | 状态 | 类别 | 原因 |', '| --- | --- | --- | --- |']
        lines += [f'| {_text(item.get("stage"))} | {_text(item.get("status"))} | '
                  f'{_text(item.get("kind"))} | {_text(item.get("reason"))} |'
                  for item in unavailable]
        lines.append('')
        lines.append('`not_run` 表示该阶段尚未执行，`incomplete`/`failed` 表示阶段已尝试但未产出结论；'
                     '两类都不会以 0、空数组或“无问题”占位。')
        lines.append('')
    findings_abstentions = abstentions.get('findings') or []
    rejected = abstentions.get('rejected_findings') or []
    if findings_abstentions or rejected:
        lines += [f'- Finding 弃权 {len(findings_abstentions)} 条（候选没有可解析证据，不生成 Finding）。',
                  f'- Finding 被拒 {len(rejected)} 条（不合约，未发布）。', '']
    return lines


def _policy_label(policy):
    """A short identifier for a versioned Measurement Policy."""
    if not isinstance(policy, dict):
        return _text(policy)
    for key in ('policy_id', 'name', 'policy', 'processor'):
        if policy.get(key):
            version = policy.get('policy_version') or policy.get('version')
            return _text(f'{policy[key]}/{version}' if version else policy[key])
    return _text(json.dumps(policy, ensure_ascii=False, sort_keys=True))


def _provenance_section(run, workbench, judge_document):
    manifest = run.manifest
    provenance = workbench.get('provenance') or {}
    lines = ['', '## 溯源与版本', '', '| 项目 | 值 |', '| --- | --- |',
             f'| Run | {_text(manifest.get("run_id"))} |',
             f'| AnalysisRevision | {_text(workbench.get("analysis_id"))} |',
             f'| 记录原始 SHA256 | {_text(manifest.get("original_sha256"))} |',
             f'| 选定音频 | {_text((workbench.get("audio") or {}).get("artifact_id"))} '
             f'({_text((workbench.get("audio") or {}).get("kind"))}, '
             f'{_text((workbench.get("audio") or {}).get("sha256"))}) |',
             f'| 音频时长 | {_fmt_ms((workbench.get("audio") or {}).get("duration_ms"))} |']
    for policy in provenance.get('policies') or []:
        lines.append(f'| policy（{_text(policy.get("document"))}） | {_policy_label(policy.get("policy"))} |')
    processors = provenance.get('processors') or []
    if processors:
        lines.append(f'| processors | {_text(", ".join(processors))} |')
    routes = (provenance.get('model_configuration') or {}).get('routes') or {}
    for route, value in sorted(routes.items()):
        lines.append(f'| route {_text(route)} | {_text(value.get("provider"))} / '
                     f'{_text(value.get("model"))} / {_text(value.get("protocol"))} |')
    for invocation in provenance.get('provider_invocations') or []:
        lines.append(f'| invocation {_text(invocation.get("invocation_id"))} | '
                     f'{_text(invocation.get("provider"))} / {_text(invocation.get("model"))} / '
                     f'status={_text(invocation.get("status"))} / '
                     f'failure={_text(invocation.get("failure_code"))} |')
    results = (judge_document or {}).get('results') or []
    if results:
        lines.append(f'| Judge 结果数 | {len(results)} |')
    policies = [policy.get('policy') for policy in provenance.get('policies') or []
                if isinstance(policy.get('policy'), dict)]
    if policies:
        lines += ['', 'Measurement Policy 快照：', '', '```json',
                  json.dumps(policies, ensure_ascii=False, indent=2, sort_keys=True), '```']
    lines += ['', 'Provider/模型只以引用与版本记录出现；报告不包含密钥、完整签名 URL 或请求参数。', '']
    return lines


def build_report_payload(run, workbench=None):
    """Assemble the revision-scoped report payload from persisted documents."""
    from .workbench import REPORT_ARTIFACT_KINDS, build_workbench

    manifest = run.manifest
    workbench = build_workbench(run.directory) if workbench is None else workbench
    # The report describes the evidence chain, not itself: listing its own output
    # would make regenerating an already-reviewed revision change its bytes.
    artifacts = [item for item in manifest['artifacts']
                 if item.get('kind') not in REPORT_ARTIFACT_KINDS]
    classes = _classify(artifacts)
    judge_document, judge_status = _load_checked(run.analysis, 'judge-results.json')
    judge_document = _data(judge_document, run.analysis)
    integrity = dict(workbench.get('evidence_integrity') or {})
    if judge_status == 'unreadable':
        integrity['status'] = 'incomplete'
        integrity['unreadable_documents'] = sorted(
            set(integrity.get('unreadable_documents') or []) | {'judge-results.json'})
    stages = {key: value for key, value in manifest['stages'].items() if key != 'report'}
    return {
        'schema_version': '1.0.0',
        'report_kind': 'import_stage_status',
        'report_scope': 'analysis_revision',
        'run_id': manifest['run_id'],
        'analysis_id': manifest['analysis_id'],
        'execution_kind': manifest['execution_kind'],
        'analysis_status': manifest['status'],
        'profile': manifest['profile'],
        'original_sha256': manifest['original_sha256'],
        'stages': stages,
        'artifacts': artifacts,
        'conclusions': [],
        'device_performance': 'insufficient_evidence',
        'gate': workbench.get('gate') or {},
        'revision': workbench.get('revision'),
        'revision_history': workbench.get('revision_history') or [],
        'workbench_document_id': workbench.get('document_id'),
        'evidence_classes': classes,
        'evidence_index': workbench.get('regions') or [],
        'metrics': workbench.get('metrics') or [],
        'findings_summary': workbench.get('findings') or [],
        'unavailable': workbench.get('unavailable') or [],
        'abstentions': workbench.get('abstentions') or {},
        'evidence_integrity': integrity,
        'provenance': workbench.get('provenance') or {},
        'note': ('Revision-scoped report. Provisional until a complete manual speaker-role '
                 'decision exists; an unrun, failed or abstaining stage never becomes a '
                 'conclusion or a zero.'),
    }


def render_report_markdown(run, payload, workbench):
    manifest = run.manifest
    judge_document = _data(_load(run.analysis, 'judge-results.json'), run.analysis)
    stages = payload['stages']
    lines = ['# 录音导入与分析状态', '', f'Run：{manifest["run_id"]}',
             f'Analysis：{manifest["analysis_id"]}', f'输入标记：{manifest["execution_kind"]}', '',
             '**当前为导入阶段报告；设备表现：insufficient_evidence。**',
             '未完成的说话人、轮次、事件和语义分析不会被当作通过或无问题。']
    if not (payload['gate'] or {}).get('role_dependent_available'):
        lines.append('')
        lines.append(f'**provisional 视图：** 人工角色确认未完成（{_text((payload["gate"] or {}).get("status"))}）。'
                     '本报告只展示导入与诊断证据，不含依赖 tester/device 角色的正式结论。')
    lines += ['', '| 信息 | 值 |', '| --- | --- |']
    lines += [f'| {_text(key)} | {_text(value) if value is not None else "未知"} |'
              for key, value in manifest['profile'].items()]
    lines += ['', '| 阶段 | 状态 | 说明 |', '| --- | --- | --- |']
    lines += [f'| {key} | {stage["status"]} | {_text(stage["reason"] or "已完成本阶段处理")} |'
              for key, stage in stages.items()]
    lines += _revision_section(payload)
    lines += _alignment_section(run)
    lines += _metrics_section(workbench)
    lines += _evidence_class_section(payload['evidence_classes'])
    lines += _evidence_index_section(workbench)
    lines += _availability_section(workbench, payload)
    lines += _provenance_section(run, workbench, judge_document)
    lines += ['', '## 本地证据文件', '', '| Artifact | 文件 | SHA256 |', '| --- | --- | --- |']
    for item in payload['artifacts']:
        link = quote('../../' + item['path'], safe='/')
        lines.append(f'| {item["artifact_id"]} | [{_text(item["kind"])}]({link}) | {item["sha256"]} |')
    lines += ['', '原始文件与派生文件分别保留，关系及处理器版本见 manifest.json。',
              '每个 revision 保存自己的报告；保存新的人工角色决定不会覆盖本文件。', '']
    return '\n'.join(lines)


def write_import_report(run):
    """Write this revision's ``report.json``/``report.md``.

    The projection is built once and shared by both files, so the report and the
    browser Evidence Workbench cannot disagree about geometry or observed values.
    """
    from .workbench import build_workbench

    workbench = build_workbench(run.directory)
    payload = build_report_payload(run, workbench)
    json_path = run.analysis / 'report.json'
    write_json(json_path, payload)
    markdown = run.analysis / 'report.md'
    markdown.write_text(render_report_markdown(run, payload, workbench), encoding='utf-8')
    return [run.register(json_path, 'report_json', processor='revision_evidence_report:1.0.0'),
            run.register(markdown, 'report_markdown', processor='revision_evidence_report:1.0.0')], payload, None
