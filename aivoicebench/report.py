"""Evidence-linked Markdown + JSON report renderer.

Takes all pipeline outputs (profile, fused segments, turns, timeline,
metrics, judge results, findings) and produces:
- report.md: human-readable Markdown with audio time ranges
- report.json: structured data for programmatic consumption

Every finding, metric, and event links back to audio timestamps and
processor/model versions for full traceability.
"""

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from .runner import write_json


def _fmt_ms(ms):
    """Format milliseconds as seconds with 2 decimal places."""
    if ms is None:
        return 'N/A'
    return f'{ms / 1000:.2f}s'


def _fmt_range(start, end):
    """Format a time range."""
    return f'{_fmt_ms(start)} – {_fmt_ms(end)}'


def _fmt_confidence(confidence):
    """Render a confidence value, or an explicit unknown marker.

    A derived event legitimately carries no confidence number, so an absent value
    must render as unknown. It must never be formatted as `0.00`, which would
    present "no defensible number" as a measured zero.
    """
    if confidence is None:
        return '—'
    return f'{confidence:.2f}'


def _esc(text):
    """Escape text for Markdown table cells."""
    if text is None:
        return ''
    return str(text).replace('|', '\\|').replace('\n', ' ').replace('\r', '')


def render_markdown(profile, fused_doc, turns_doc, timeline, metrics_result,
                    judge_data, findings):
    """Render a complete Markdown analysis report."""
    lines = []
    lines.append('# AIVoiceBench 录音分析报告')
    lines.append('')
    lines.append(f'生成时间：{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
    lines.append('')

    # Run summary
    lines.append('## 运行摘要')
    lines.append('')
    lines.append('| 项目 | 值 |')
    lines.append('| --- | --- |')
    lines.append(f'| 执行类型 | {timeline.get("execution_kind", "unknown")} |')
    lines.append(f'| 时间线状态 | {timeline.get("status", "unknown")} |')
    lines.append(f'| 融合段数 | {len(fused_doc.get("segments", []))} |')
    lines.append(f'| 对话轮次 | {len(turns_doc.get("turns", []))} |')
    lines.append(f'| 检测事件 | {len(timeline.get("events", []))} |')
    lines.append(f'| 指标数 | {len(metrics_result.get("metrics", []))} |')
    lines.append(f'| 评估结果 | {len(judge_data.get("results", []))} |')
    lines.append(f'| Finding 数 | {len(findings)} |')
    lines.append('')

    # Device profile
    if profile:
        lines.append('## 设备信息')
        lines.append('')
        lines.append('| 项目 | 值 |')
        lines.append('| --- | --- |')
        for key in ('device', 'hardware', 'firmware', 'model', 'prompt',
                     'supplier', 'environment', 'notes'):
            val = profile.get(key)
            if val:
                lines.append(f'| {key} | {_esc(val)} |')
        lines.append('')

    # Fused segments
    segs = fused_doc.get('segments', [])
    if segs:
        lines.append('## 音频分段')
        lines.append('')
        lines.append('| 段ID | 开始 | 结束 | 时长 | 说话人 | 置信度 | 文本 |')
        lines.append('| --- | --- | --- | --- | --- | --- | --- |')
        for s in segs:
            dur = s['end_ms'] - s['start_ms']
            text = s.get('text') or ''
            role = s.get('speaker_role', 'unknown')
            lines.append(f'| {s["segment_id"]} | {_fmt_ms(s["start_ms"])} | {_fmt_ms(s["end_ms"])} '
                        f'| {dur:.0f}ms | {role} | {_fmt_confidence(s.get("speaker_confidence"))} | {_esc(text)} |')
        lines.append('')
        lines.append(f'> 说话人归因策略：{fused_doc.get("attribution", {}).get("strategy", "unknown")} '
                     f'(置信度 {_fmt_confidence(fused_doc.get("attribution", {}).get("confidence"))})')
        lines.append('')

    # Turns
    turns = turns_doc.get('turns', [])
    if turns:
        lines.append('## 对话轮次')
        lines.append('')
        lines.append('| 轮次 | 测试者语音 | 设备响应 | 响应ID | 打断 | 重叠 |')
        lines.append('| --- | --- | --- | --- | --- | --- |')
        for t in turns:
            ts = _fmt_range(t.get('tester_speech_start_ms'), t.get('tester_speech_end_ms')) if t.get('tester_speech_start_ms') else 'N/A'
            ds = _fmt_range(t.get('device_speech_start_ms'), t.get('device_speech_end_ms')) if t.get('device_speech_start_ms') else 'N/A'
            lines.append(f'| {t["turn_id"]} | {ts} | {ds} | {t.get("response_id") or "N/A"} '
                        f'| {"是" if t.get("has_interruption") else "否"} '
                        f'| {"是" if t.get("has_overlap") else "否"} |')
        lines.append('')

    # Events
    events = timeline.get('events', [])
    if events:
        lines.append('## 事件时间线')
        lines.append('')
        lines.append('| 事件ID | 类型 | 时间 | 轮次 | 来源 | 置信度 |')
        lines.append('| --- | --- | --- | --- | --- | --- |')
        for e in events:
            etype = e.get('type', '')
            t = _fmt_ms(e.get('start_ms'))
            if e.get('end_ms') != e.get('start_ms'):
                t = _fmt_range(e.get('start_ms'), e.get('end_ms'))
            lines.append(f'| {e.get("event_id", "")} | {etype} | {t} '
                        f'| {e.get("turn_id") or "—"} | {e.get("source", "")} '
                        f'| {_fmt_confidence(e.get("confidence"))} |')
        lines.append('')

    # Metrics
    metrics = metrics_result.get('metrics', [])
    if metrics:
        lines.append('## 指标')
        lines.append('')
        lines.append('| 指标 | 值 | 单位 | 状态 | 轮次 |')
        lines.append('| --- | --- | --- | --- | --- |')
        for m in metrics:
            val = m.get('value')
            if val is not None:
                if m.get('unit') == 'boolean':
                    val_str = '是' if val else '否'
                elif m.get('unit') == 'ratio':
                    val_str = f'{val:.1%}'
                else:
                    val_str = f'{val}'
            else:
                val_str = 'N/A'
            lines.append(f'| {m.get("name", "")} | {val_str} | {m.get("unit", "")} '
                        f'| {m.get("status", "")} | {m.get("turn_id") or "—"} |')
        lines.append('')
        lines.append(f'> 指标总状态：{metrics_result.get("status", "unknown")}')
        lines.append('')

    # Judge results
    results = judge_data.get('results', [])
    if results:
        lines.append('## LLM 语义评估')
        lines.append('')
        lines.append('| 维度 | 决策 | 置信度 | 状态 | 详情 |')
        lines.append('| --- | --- | --- | --- | --- |')
        for r in results:
            dim = r.get('dimension', '')
            dec = r.get('decision', '')
            conf = r.get('confidence', 0)
            st = r.get('status', '')
            extra = ''
            if r.get('meaningful_response_start_ms') is not None:
                extra = f'有意义响应开始：{_fmt_ms(r["meaningful_response_start_ms"])}'
            elif r.get('intent_label'):
                extra = f'意图：{r["intent_label"]}'
            elif r.get('feedback_type'):
                extra = f'反馈类型：{r["feedback_type"]} ({_fmt_range(r.get("feedback_start_ms"), r.get("feedback_end_ms"))})'
            elif r.get('score') is not None:
                extra = f'评分：{r["score"]:.2f}'
            elif r.get('finding_severity'):
                extra = f'严重性：{r["finding_severity"]} 疑似层：{r.get("suspected_layer", "?")}'
            lines.append(f'| {dim} | {dec} | {conf:.2f} | {st} | {_esc(extra)} |')
        lines.append('')
        invocations = judge_data.get('invocations', [])
        if invocations:
            lines.append(f'> LLM 调用次数：{len(invocations)} (provider: {invocations[0].get("provider", "?")}, '
                         f'model: {invocations[0].get("model", "?")})')
            lines.append('')

    # Findings
    if findings:
        lines.append('## Findings')
        lines.append('')
        for f in findings:
            sev = f.get('severity') or '—'
            lines.append(f'### {f["title"]} [{sev}]')
            lines.append('')
            lines.append(f'- **状态**：{f["status"]} (需人工确认)')
            lines.append(f'- **描述**：{f["description"]}')
            lines.append(f'- **预期行为**：{f["expected_behavior"]}')
            lines.append(f'- **实际行为**：{f["actual_behavior"]}')
            lines.append(f'- **置信度**：{f["confidence"]:.2f}')
            lines.append(f'- **归因状态**：{f["attribution_status"]}')
            if f.get('suspected_layers'):
                lines.append(f'- **疑似层**：{", ".join(f["suspected_layers"])}')
                lines.append(f'- **归因置信度**：{f.get("attribution_confidence", 0):.2f}')
                lines.append(f'- **需日志验证**：{"是" if f.get("requires_log_verification") else "否"}')
            lines.append(f'- **证据引用**：{", ".join(f.get("evidence_ids", []))}')
            if f.get('event_ids'):
                lines.append(f'- **事件引用**：{", ".join(f["event_ids"])}')
            if f.get('metric_ids'):
                lines.append(f'- **指标引用**：{", ".join(f["metric_ids"])}')
            lines.append(f'- **人工审核**：{f.get("human_review", {}).get("status", "unknown")}')
            lines.append('')
    else:
        lines.append('## Findings')
        lines.append('')
        lines.append('暂无可确认的问题结论；这不代表设备已通过评测。请结合指标状态与证据完整性复核。')
        lines.append('')

    # Evidence
    evidence = timeline.get('evidence', [])
    if evidence:
        lines.append('## 证据')
        lines.append('')
        lines.append('| 证据ID | 音频区间 | 来源 | 置信度 |')
        lines.append('| --- | --- | --- | --- |')
        for e in evidence:
            lines.append(f'| {e.get("evidence_id", "")} | {_fmt_range(e.get("start_ms"), e.get("end_ms"))} '
                         f'| {e.get("source", "")} | {_fmt_confidence(e.get("confidence"))} |')
        lines.append('')

    # Provenance
    lines.append('## 溯源')
    lines.append('')
    lines.append('| 项目 | 值 |')
    lines.append('| --- | --- |')
    lines.append(f'| 声学分割 | {fused_doc.get("source", {}).get("acoustic_document_id", "?")} |')
    lines.append(f'| 说话人归因 | {fused_doc.get("attribution", {}).get("strategy", "?")} |')
    if results:
        inv = judge_data.get('invocations', [{}])[0]
        lines.append(f'| LLM provider | {inv.get("provider", "?")} |')
        lines.append(f'| LLM model | {inv.get("model", "?")} |')
        lines.append(f'| prompt_version | {inv.get("prompt_version", "?")} |')
    lines.append(f'| 音频 SHA256 | {fused_doc.get("source", {}).get("audio_sha256", "?")[:16]}... |')
    lines.append(f'| 音频时长 | {_fmt_ms(fused_doc.get("source", {}).get("duration_ms"))} |')
    lines.append('')
    lines.append('---')
    lines.append('*本报告由 AIVoiceBench 自动生成。所有结论可回溯到对应音频时间区间。'
                 '未确认的 Findings 需要人工审核。*')

    return '\n'.join(lines)


def render_report(profile, fused_doc, turns_doc, timeline, metrics_result,
                  judge_data, findings, output_dir):
    """Render both Markdown and JSON reports to output_dir."""
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    md = render_markdown(profile, fused_doc, turns_doc, timeline,
                         metrics_result, judge_data, findings)
    (out / 'report.md').write_text(md, encoding='utf-8')

    report_json = {
        'schema_version': '1.0.0',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'profile': profile,
        'run_summary': {
            'execution_kind': timeline.get('execution_kind'),
            'status': timeline.get('status'),
            'segment_count': len(fused_doc.get('segments', [])),
            'turn_count': len(turns_doc.get('turns', [])),
            'event_count': len(timeline.get('events', [])),
            'metric_count': len(metrics_result.get('metrics', [])),
            'judge_result_count': len(judge_data.get('results', [])),
            'finding_count': len(findings),
        },
        'fused_segments': fused_doc,
        'turns': turns_doc,
        'timeline': timeline,
        'metrics': metrics_result,
        'judge_results': judge_data,
        'findings': findings,
    }
    write_json(out / 'report.json', report_json)
    return out / 'report.md', out / 'report.json'


def render_report_from_files(output_dir, profile_path=None,
                              fused_path=None, turns_path=None,
                              timeline_path=None, metrics_path=None,
                              judge_path=None, findings_path=None):
    """Read all pipeline JSON files and render a report."""
    def load(path):
        if path is None:
            return {}
        p = Path(path)
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding='utf-8'))

    profile = load(profile_path) or {}
    fused_doc = load(fused_path) or {'segments': [], 'source': {}, 'attribution': {}}
    turns_doc = load(turns_path) or {'turns': []}
    timeline = load(timeline_path) or {'events': [], 'evidence': []}
    metrics_result = load(metrics_path) or {'metrics': []}
    judge_data = load(judge_path) or {'results': [], 'invocations': []}
    findings_data = load(findings_path) or {'findings': []}
    findings = findings_data.get('findings', []) if isinstance(findings_data, dict) else findings_data

    return render_report(profile, fused_doc, turns_doc, timeline,
                         metrics_result, judge_data, findings, output_dir)
