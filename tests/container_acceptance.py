"""Acceptance run against the candidate Docker image (PRD-F020/F023).

Executed **inside** the released image against the image's own installed
``aivoicebench`` package: the shipped code is what is being exercised. The only
injected piece is a deterministic synthetic TTS provider, which is copied in
with the test files for the duration of the run and is never part of the image
or of the product's normal startup path.

Prints a machine-readable summary so the release log records exactly what ran.
"""

import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from aivoicebench import api  # noqa: E402
from aivoicebench.model_settings import RunProviders  # noqa: E402
from aivoicebench.version import VERSION  # noqa: E402
from aivoicebench.voice_test import VoiceTestManager  # noqa: E402
from browser_server import SyntheticTTS  # noqa: E402

CHECKS = []


def check(name, condition, detail=''):
    CHECKS.append({'check': name, 'ok': bool(condition), 'detail': str(detail)[:300]})
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ''),
          flush=True)


def build_client(output_root):
    tts = SyntheticTTS(output_root, first_phrase_ms=250, phrase_ms=150,
                       slow_phrase_ms=1500)
    manager = VoiceTestManager(output_root)
    manager._providers = RunProviders(tts=lambda root: tts)
    for item in (patch.object(api, '_voice_test_manager', manager),
                 patch.object(api, 'OUTPUT_ROOT', output_root)):
        item.start()
    return TestClient(api.app), manager, tts


def new_fixed_session(client, phrases, **extra):
    body = {'mode': 'fixed', 'phrases': phrases, 'device': 'acceptance',
            'no_response_timeout_ms': 1500}
    body.update(extra)
    created = client.post('/api/voice-test/sessions', json=body)
    assert created.status_code == 200, created.text
    session_id = created.json()['session_id']
    synth = client.post(f'/api/voice-test/sessions/{session_id}/synthesize')
    assert synth.status_code == 200, synth.text
    return session_id


def main():
    output_root = Path('/data/output/acceptance')
    output_root.mkdir(parents=True, exist_ok=True)
    client, manager, tts = build_client(output_root)

    check('image reports the expected version', VERSION.startswith('0.'),
          f'VERSION={VERSION}')
    health = client.get('/health').json()
    check('container /health matches the application version',
          health.get('version') == VERSION, json.dumps(health))
    static = client.get('/static/voice_test.js')
    check('served UI asset is available and contains the control layer',
          static.status_code == 200 and 'controlState' in static.text,
          f'{static.status_code} {len(static.content)} bytes')

    # ---------------------------------------------------------------- 1. three rounds
    phrases = ['第一句：你好', '第二句：南京天气', '第三句：北京呢']
    session_id = new_fixed_session(client, phrases)
    played = []
    with client.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws') as ws:
        ws.send_json({'type': 'start'})
        message = ws.receive_json()
        while message.get('type') == 'play':
            played.append(message['phrase_index'])
            turn_id = message['turn_id']
            ws.send_json({'type': 'playback_started', 'turn_id': turn_id})
            ws.send_json({'type': 'device_speech_start', 'turn_id': turn_id})
            assert ws.receive_json()['type'] == 'listening'
            ws.send_json({'type': 'device_speech_end', 'turn_id': turn_id})
            message = ws.receive_json()
    check('1. three rounds advance in order',
          played == [0, 1, 2], f'played={played}')
    check('1. run completes after the last observed response',
          message.get('type') == 'complete' and message.get('reason') == 'all_phrases_done',
          json.dumps(message))

    record = client.get(f'/api/voice-test/sessions/{session_id}/execution-record').json()
    kinds = [e['kind'] for e in record['events']]
    run = [r for r in record['runs'] if r['current']][0]
    check('1. control record is written for every turn',
          len(run['turns']) == 3 and kinds.count('play_issued') == 3
          and kinds.count('playback_started') == 3
          and kinds.count('observation_speech_end') == 3,
          f"turns={len(run['turns'])} kinds={sorted(set(kinds))}")
    check('1. play instruction and browser report stay separate events',
          all(t['play_issued_at'] and t['playback_started_at'] for t in run['turns'])
          and all(t['observation_basis'] == 'browser_vad_rms' for t in run['turns']))

    # --------------------------------------------------- 2. stop during playback
    session_id = new_fixed_session(client, ['[slow] 请讲一个很长的故事', '第二句'])
    with client.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws') as ws:
        ws.send_json({'type': 'start'})
        play = ws.receive_json()
        ws.send_json({'type': 'playback_started', 'turn_id': play['turn_id']})
        ws.send_json({'type': 'playback_cancelled', 'turn_id': play['turn_id'],
                      'reason': 'user_stop'})
        ws.send_json({'type': 'stop'})
        stopped = ws.receive_json()
    check('2. stop is acknowledged', stopped.get('type') == 'stopped', json.dumps(stopped))
    state = manager.get_session(session_id)
    check('2. stop leaves the run stopped and does not advance',
          state.status == 'stopped' and state.current_phrase_index == 0
          and state.turns[0].status == 'cancelled'
          and state.turns[0].playback_ended_at is None,
          f'{state.status} idx={state.current_phrase_index} turn={state.turns[0].status}')
    record = client.get(f'/api/voice-test/sessions/{session_id}/execution-record').json()
    stop_kinds = [e['kind'] for e in record['events']]
    check('2. cancellation is recorded and no natural end is claimed',
          'playback_cancelled' in stop_kinds and 'playback_ended' not in stop_kinds
          and 'observation_speech_end' not in stop_kinds, str(stop_kinds))

    # ------------------------------------------------------ 3. no-response timeout
    session_id = new_fixed_session(client, ['超时用例'])
    with client.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws') as ws:
        ws.send_json({'type': 'start'})
        play = ws.receive_json()
        ws.send_json({'type': 'playback_ended', 'turn_id': play['turn_id']})
        ws.send_json({'type': 'observation_timeout', 'turn_id': play['turn_id'],
                      'wait_ms': 1500})
        timed_out = ws.receive_json()
    check('3. timeout stops the run with an explicit reason',
          timed_out.get('type') == 'stopped'
          and timed_out.get('reason') == 'no_response_timeout', json.dumps(timed_out))
    record = client.get(f'/api/voice-test/sessions/{session_id}/execution-record').json()
    turn = [r for r in record['runs'] if r['current']][0]['turns'][0]
    kinds = [e['kind'] for e in record['events']]
    check('3. a timeout is never recorded as a completed answer',
          turn['observation'] == 'no_response'
          and turn['closure_reason'] == 'no_response_timeout'
          and 'observation_timeout' in kinds
          and 'observation_speech_end' not in kinds
          and 'observation_speech_start' not in kinds,
          json.dumps({'observation': turn['observation'],
                      'closure_reason': turn['closure_reason']}))

    # ------------------------------------------------------ 4. failure then restart
    session_id = new_fixed_session(client, ['异常用例'])
    with client.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws') as ws:
        ws.send_json({'type': 'start'})
        play = ws.receive_json()
        ws.send_json({'type': 'playback_failed', 'turn_id': play['turn_id'],
                      'reason': 'audio_error'})
        failed = ws.receive_json()
        check('4. a playback failure is reported instead of hanging',
              failed.get('type') == 'failed', json.dumps(failed))
        ws.send_json({'type': 'start'})
        restarted = ws.receive_json()
    check('4. the same session can be restarted after the failure',
          restarted.get('type') == 'play' and restarted.get('phrase_index') == 0,
          json.dumps(restarted))
    record = client.get(f'/api/voice-test/sessions/{session_id}/execution-record').json()
    archived = [r for r in record['runs'] if not r['current']]
    check('4. the failed run is archived rather than erased',
          len(archived) == 1 and archived[0]['status'] == 'failed'
          and archived[0]['turns'][0]['status'] == 'failed',
          json.dumps({'runs': [r['status'] for r in record['runs']]}))

    record_path = output_root / 'voice-test' / session_id / 'execution-record.json'
    check('4. the control record is persisted in the session directory',
          record_path.is_file(), str(record_path))

    client.close()
    failed_checks = [c for c in CHECKS if not c['ok']]
    summary = {'version': VERSION, 'total': len(CHECKS),
               'passed': len(CHECKS) - len(failed_checks),
               'failed': [c['check'] for c in failed_checks]}
    print('ACCEPTANCE_SUMMARY ' + json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failed_checks else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print('ACCEPTANCE_SUMMARY ' + json.dumps({'error': 'exception'}), flush=True)
        sys.exit(2)
