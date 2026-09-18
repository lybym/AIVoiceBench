"""Honest ingestion/status report. Full conversation findings remain a later processor."""

from html import escape
import json
from urllib.parse import quote

from .runner import write_json


def _text(value):
    return escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ').replace('`', '&#96;')


def _alignment_section(run):
    """Explain why role-dependent metrics are absent, using the recorded evidence.

    A blank metric table must be traceable to a count of unmatched acoustic
    duration, an unmatched speaker duration or an unconfirmed role — never left to
    look like the report simply omitted data.
    """
    analysis = run.analysis
    alignment = None
    path = analysis / 'alignment.json'
    if path.is_file():
        alignment = json.loads(path.read_text(encoding='utf-8'))
    fused = None
    fused_path = analysis / 'fused-segments.json'
    if fused_path.is_file():
        envelope = json.loads(fused_path.read_text(encoding='utf-8'))
        fused = envelope.get('data')
    metrics_path = analysis / 'metrics.json'
    metrics = None
    if metrics_path.is_file():
        envelope = json.loads(metrics_path.read_text(encoding='utf-8'))
        metrics = envelope.get('data')
    timeline_path = analysis / 'timeline.json'
    timeline = None
    if timeline_path.is_file():
        envelope = json.loads(timeline_path.read_text(encoding='utf-8'))
        timeline = envelope.get('data')

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


def write_import_report(run):
    manifest = run.manifest
    stages = {key: value for key, value in manifest['stages'].items() if key != 'report'}
    payload = {'schema_version': '1.0.0', 'report_kind': 'import_stage_status',
        'run_id': manifest['run_id'], 'analysis_id': manifest['analysis_id'],
        'execution_kind': manifest['execution_kind'], 'analysis_status': manifest['status'],
        'profile': manifest['profile'], 'original_sha256': manifest['original_sha256'],
        'stages': stages, 'artifacts': list(manifest['artifacts']),
        'conclusions': [], 'device_performance': 'insufficient_evidence',
        'note': 'Import checkpoint only. No automatic speaker/turn/event/Judge result is claimed. Final stage state is in manifest.json.'}
    json_path = run.analysis / 'report.json'
    write_json(json_path, payload)
    lines = ['# 录音导入与分析状态', '', f'Run：{manifest["run_id"]}',
             f'Analysis：{manifest["analysis_id"]}', f'输入标记：{manifest["execution_kind"]}', '',
             '**当前为导入阶段报告；设备表现：insufficient_evidence。**',
             '未完成的说话人、轮次、事件和语义分析不会被当作通过或无问题。', '',
             '| 信息 | 值 |', '| --- | --- |']
    lines += [f'| {_text(key)} | {_text(value) if value is not None else "未知"} |' for key, value in manifest['profile'].items()]
    lines += ['', '| 阶段 | 状态 | 说明 |', '| --- | --- | --- |']
    lines += [f'| {key} | {stage["status"]} | {_text(stage["reason"] or "已完成本阶段处理")} |' for key, stage in stages.items()]
    lines += _alignment_section(run)
    lines += ['', '## 本地证据文件', '', '| Artifact | 文件 | SHA256 |', '| --- | --- | --- |']
    for item in manifest['artifacts']:
        link = quote('../../' + item['path'], safe='/')
        lines.append(f'| {item["artifact_id"]} | [{_text(item["kind"])}]({link}) | {item["sha256"]} |')
    lines += ['', '原始文件与派生文件分别保留，关系及处理器版本见 manifest.json。',
              '本阶段没有生成可确认的 Finding 或精确事件时间；后续分析完成后才能提供结论对应的音频区间。', '']
    markdown = run.analysis / 'report.md'
    markdown.write_text('\n'.join(lines), encoding='utf-8')
    return [run.register(json_path, 'report_json', processor='import_status_report:1.0.0'),
            run.register(markdown, 'report_markdown', processor='import_status_report:1.0.0')], payload, None
