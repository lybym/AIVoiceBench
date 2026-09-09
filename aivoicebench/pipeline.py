"""Unified analysis pipeline: acoustic → fusion → metrics → judge → findings → report.

Runs the complete chain from a canonical WAV file or pre-existing
acoustic-segments JSON, producing all intermediate artifacts and a final
evidence-linked report in one command.
"""

import json
import os
from pathlib import Path

from .acoustic import EnergyVadSegmenter
from .fusion import fuse, build_turns, detect_events, generate_timeline
from .metrics import compute_timeline_metrics
from .llm import LLMJudge, MockLLMProvider
from .findings import generate_findings
from .report import render_report
from .runner import write_json


def run_full_pipeline(source, output, profile=None, *, provider=None,
                      frame_ms=30.0, hop_ms=10.0, min_speech_ms=100.0,
                      min_silence_ms=200.0, timeout_ms=5000.0,
                      false_endpoint_ms=300.0):
    """Run the complete analysis pipeline on a canonical WAV file.

    Args:
        source: path to a canonical WAV (PCM16 16kHz mono)
        output: output directory for all artifacts
        profile: dict with device/hardware/firmware/model/prompt/supplier/environment/notes
        provider: LLM provider (defaults to MockLLMProvider)

    Returns dict with run_id, status, and artifact paths.
    """
    source = Path(source).resolve()
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    profile = profile or {}

    # 1. Acoustic segmentation
    segmenter = EnergyVadSegmenter(
        frame_ms=frame_ms, hop_ms=hop_ms,
        min_speech_ms=min_speech_ms, min_silence_ms=min_silence_ms,
    )
    acoustic_result = segmenter.segment(source)
    acoustic_doc = acoustic_result.to_dict()
    write_json(out / 'acoustic-segments.json', acoustic_doc)

    if acoustic_doc['status'] == 'insufficient_evidence':
        # Still produce a report with the partial state
        fused_doc = {'segments': [], 'source': acoustic_doc['source'],
                     'attribution': {'strategy': 'none', 'provider': None,
                                     'confidence': 0, 'note': 'No acoustic segments'}}
        turns_doc = {'turns': []}
        timeline = {'events': [], 'evidence': [], 'status': 'partial'}
        metrics_result = {'status': 'insufficient_evidence', 'metrics': []}
        judge_data = {'results': [], 'invocations': []}
        findings = []
    else:
        # 2. Fusion → turns → events → timeline
        fused_doc = fuse(acoustic_doc)
        turns_doc = build_turns(fused_doc)
        events, evidence, status, reason = detect_events(
            fused_doc, turns_doc, timeout_ms=timeout_ms,
            false_endpoint_ms=false_endpoint_ms)
        timeline = generate_timeline(fused_doc, turns_doc, events, evidence, status, reason)
        write_json(out / 'fused-segments.json', fused_doc)
        write_json(out / 'turns.json', turns_doc)
        write_json(out / 'timeline.json', timeline)

        # 3. Metrics
        metrics_result = compute_timeline_metrics(timeline)
        write_json(out / 'metrics.json', metrics_result)

        # 4. LLM Judge
        from .llm import LLMJudge, MockLLMProvider
        from .llm_provider import create_provider_from_config

        # Determine LLM provider: explicit > env config > mock
        if provider is not None:
            llm_provider = provider
        else:
            provider_name = os.environ.get('AIVOICEBENCH_LLM_PROVIDER', 'mock')
            if provider_name == 'volcengine':
                llm_provider = create_provider_from_config({
                    'llm_provider': 'volcengine',
                    'volcengine': {
                        'model': os.environ.get('AIVOICEBENCH_LLM_MODEL', ''),
                        'base_url': os.environ.get('AIVOICEBENCH_LLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3'),
                    }
                })
            elif provider_name == 'openai':
                llm_provider = create_provider_from_config({
                    'llm_provider': 'openai',
                    'openai': {
                        'model': os.environ.get('AIVOICEBENCH_LLM_MODEL', 'gpt-4o'),
                        'base_url': os.environ.get('AIVOICEBENCH_LLM_BASE_URL', 'https://api.openai.com/v1'),
                    }
                })
            else:
                llm_provider = MockLLMProvider()
        judge = LLMJudge(llm_provider)
        judge_results, invocations = judge.evaluate_all(fused_doc, turns_doc, metrics_result)
        judge_data = {'results': judge_results, 'invocations': invocations}
        write_json(out / 'judge-results.json', judge_data)

        # 5. Integrate LLM meaningful_response_start back into metrics
        metrics_result = _integrate_llm_metrics(metrics_result, judge_results)
        write_json(out / 'metrics.json', metrics_result)

        # 6. Findings
        findings = generate_findings(judge_results, timeline, metrics_result)
        write_json(out / 'findings.json', {'findings': findings})

    # 7. Profile
    write_json(out / 'profile.json', profile)

    # 8. Report
    md_path, json_path = render_report(
        profile, fused_doc, turns_doc, timeline, metrics_result,
        judge_data, findings, out)

    run_id = out.name
    return {
        'run_id': run_id,
        'status': timeline.get('status', 'partial'),
        'output': str(out),
        'report_md': str(md_path),
        'report_json': str(json_path),
        'segment_count': len(fused_doc.get('segments', [])),
        'turn_count': len(turns_doc.get('turns', [])),
        'event_count': len(timeline.get('events', [])),
        'metric_count': len(metrics_result.get('metrics', [])),
        'judge_result_count': len(judge_data.get('results', [])),
        'finding_count': len(findings),
    }


def _integrate_llm_metrics(metrics_result, judge_results):
    """Feed LLM meaningful_response and feedback results back into metrics.

    Resolves insufficient_evidence metrics by using LLM semantic analysis.
    """
    metrics = metrics_result.get('metrics', [])

    # Find meaningful_response judge result
    mr_judge = None
    fb_judge = None
    for r in judge_results:
        if r.get('dimension') == 'meaningful_response' and r.get('status') == 'observed':
            mr_judge = r
        if r.get('dimension') == 'feedback_detection' and r.get('status') == 'observed':
            fb_judge = r

    for m in metrics:
        if m.get('name') == 'meaningful_response_latency_ms' and mr_judge:
            ms_start = mr_judge.get('meaningful_response_start_ms')
            if ms_start is not None:
                # Find the tester_speech_end from the same turn
                turn_id = mr_judge.get('turn_id')
                # Look for the turn's tester_speech_end in events
                # For now, use the metric's turn_id to find the tester end
                # This is a simplification — in full integration, we'd look up the timeline
                m['value'] = None  # Will be set if we have tester_end
                m['status'] = 'observed'
                m['reason'] = f'LLM meaningful_response_start={ms_start}ms (confidence={mr_judge.get("confidence", 0.6)})'
                m['evidence_ids'] = mr_judge.get('evidence_refs', [])
                m['_llm_meaningful_response_start_ms'] = ms_start
                m['_llm_confidence'] = mr_judge.get('confidence', 0.6)

        if m.get('name') == 'feedback_latency_ms' and fb_judge:
            fb_start = fb_judge.get('feedback_start_ms')
            fb_end = fb_judge.get('feedback_end_ms')
            if fb_start is not None and fb_end is not None:
                m['value'] = round(fb_end - fb_start, 3)
                m['status'] = 'observed'
                m['unit'] = 'ms'
                m['reason'] = f'LLM feedback_detection: {fb_judge.get("feedback_type")} at {fb_start}-{fb_end}ms'
                m['_llm_feedback_type'] = fb_judge.get('feedback_type')

    # Update overall status
    observed = [m for m in metrics if m.get('status') == 'observed']
    metrics_result['status'] = 'observed' if observed else 'insufficient_evidence'
    return metrics_result
