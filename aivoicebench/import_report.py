"""Honest ingestion/status report. Full conversation findings remain a later processor."""

from html import escape
from urllib.parse import quote

from .runner import write_json


def _text(value):
    return escape(str(value)).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ').replace('`', '&#96;')


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
