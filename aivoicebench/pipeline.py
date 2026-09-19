"""Unified analysis pipeline: acoustic → fusion → metrics → judge → findings → report.

Runs the complete chain from a canonical WAV file or pre-existing
acoustic-segments JSON, producing all intermediate artifacts and a final
evidence-linked report in one command.

The Judge stage is evidence-bound end to end: it may only cite objects of the
Timeline it is given, its timing comes from measured anchors it selected, and its
output is validated before it becomes metrics or findings input. A Judge artifact
that fails its own contract stops the run instead of being written as evidence.
"""

import os
from pathlib import Path

from .acoustic import resolve_segmenter
from .fusion import fuse, build_turns, detect_events, generate_timeline
from .metrics import compute_timeline_metrics, run_status
from .llm import LLMJudge, UnavailableLLMProvider
from .findings import generate_findings_document
from .report import render_report
from .runner import write_json
from .semantic_evidence import semantic_evidence_records
from .validation import judge_document_errors, semantic_evidence_errors


def run_full_pipeline(source, output, profile=None, *, provider=None,
                      frame_ms=30.0, hop_ms=10.0, min_speech_ms=100.0,
                      min_silence_ms=200.0, timeout_ms=5000.0,
                      false_endpoint_ms=300.0, vad=None, policy=None):
    """Run the complete analysis pipeline on a canonical WAV file.

    Args:
        source: path to a canonical WAV (PCM16 16kHz mono)
        output: output directory for all artifacts
        profile: dict with device/hardware/firmware/model/prompt/supplier/environment/notes
        provider: LLM provider (defaults to UnavailableLLMProvider)
        vad: acoustic-boundary provider family ('energy' or 'silero'); defaults to
            AIVOICEBENCH_ACOUSTIC_PROVIDER and then to the energy VAD. A model
            provider is never silently replaced by the energy VAD.
        policy: named Silero boundary policy version; rejected by the energy VAD.

    Returns dict with run_id, status, and artifact paths.
    """
    source = Path(source).resolve()
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    profile = profile or {}

    # 1. Acoustic segmentation
    segmenter = resolve_segmenter(
        vad, policy, frame_ms=frame_ms, hop_ms=hop_ms,
        min_speech_ms=min_speech_ms, min_silence_ms=min_silence_ms)
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

        # 3. Deterministic metrics — they take precedence wherever the question is
        # deterministic, so they are computed before any semantic judgment.
        metrics_result = compute_timeline_metrics(timeline)
        write_json(out / 'metrics.json', metrics_result)

        # 4. LLM Judge
        from .llm_provider import create_provider_from_config

        # Determine LLM provider: explicit > env config > unavailable
        if provider is not None:
            llm_provider = provider
        else:
            provider_name = os.environ.get('AIVOICEBENCH_LLM_PROVIDER', 'none')
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
            elif provider_name == 'mock':
                # A fixture is never the production default: an unconfigured run
                # must abstain, not publish fixture semantics as measurement.
                llm_provider = UnavailableLLMProvider()
            else:
                llm_provider = UnavailableLLMProvider()
        judge = LLMJudge(llm_provider)
        judge_data = judge.evaluate(fused_doc, turns_doc, metrics_result, timeline,
                                    run_id=out.name)
        _assert_valid(judge_document_errors(judge_data, timeline, turns_doc),
                      'Judge artifact violates its own contract')
        write_json(out / 'judge-results.json', judge_data)
        write_json(out / 'judge-raw.json', {
            'schema_version': '1.0.0', 'run_id': out.name,
            'invocations': judge_data['invocations'],
        })

        # 5. Constrained semantic evidence feeds the canonical semantic metrics.
        semantic_records, semantic_abstentions = semantic_evidence_records(
            judge_data, timeline, turns_doc)
        _assert_valid(semantic_evidence_errors(semantic_records, timeline, turns_doc),
                      'Semantic evidence violates the engine contract')
        judge_data['abstentions'] = (judge_data.get('abstentions') or []) + semantic_abstentions
        metrics_result = compute_timeline_metrics(timeline, semantic_evidence=semantic_records)
        # Legacy semantic-latency names remain unverified anchors; the engine keeps
        # them abstained rather than resurrecting model-authored milliseconds.
        metrics_result = _integrate_llm_metrics(metrics_result, judge_data['results'])
        write_json(out / 'metrics.json', metrics_result)

        # 6. Findings
        findings_document = generate_findings_document(judge_data, timeline, metrics_result,
                                                      run_id=out.name)
        if findings_document['rejected']:
            raise ValueError('Generated findings violate the Finding contract: '
                             + '; '.join(item['reason'] for item in findings_document['rejected']))
        findings = findings_document['findings']
        write_json(out / 'findings.json', findings_document)

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


def _assert_valid(errors, headline):
    """Stop the run when this engine's own output breaks its contract.

    Writing a document that the project's validator rejects would publish
    evidence no consumer can legitimately trust, so it is a hard error rather
    than a warning.
    """
    if errors:
        raise ValueError(headline + ': ' + '; '.join(errors))


def _integrate_llm_metrics(metrics_result, judge_results):
    """Feed LLM meaningful_response and feedback results back into metrics.

    Resolves insufficient_evidence metrics by using LLM semantic analysis.
    """
    # Until validated per-turn semantic anchor IDs are wired, retain unknowns.
    # A feedback interval is a duration, not tester-end-to-feedback latency.
    for metric in metrics_result.get('metrics', []):
        if metric.get('name') in ('meaningful_response_latency_ms', 'feedback_latency_ms'):
            metric['value'] = None
            metric['status'] = 'insufficient_evidence'
            metric['reason'] = 'Verified per-turn semantic timing anchors are unavailable'
    # One status rule for the whole engine: this surface used to derive the run
    # status with its own "observed and value is not None" test, which was one
    # null-valued observed metric away from disagreeing with the canonical
    # `compute_timeline_metrics` result on the same documents.
    metrics_result['status'] = run_status(metrics_result.get('metrics', []))
    return metrics_result
