"""Web recording import, recoverable analysis, history and audio playback."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .acoustic import EnergyVadSegmenter
from .runner import write_json, digest
from .version import VERSION
from .import_pipeline import import_recording, resume_recording
from .audio_processing import MAX_INPUT_BYTES

app = FastAPI(
    title="AIVoiceBench",
    description="AI Voice Terminal Evaluation — Recording Import & Analysis API",
    version=VERSION,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_ROOT = Path(os.environ.get("AIVOICEBENCH_OUTPUT", "artifacts"))
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

# Serve Web UI
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the Web UI."""
    html_path = _static_dir / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>AIVoiceBench API</h1><p>Web UI not found. Use /docs for API.</p>"


class HealthResponse(BaseModel):
    status: str
    version: str


class AnalysisResponse(BaseModel):
    run_id: str
    status: str
    fused_segments: list
    turns: list
    events: list
    metrics: list
    acoustic_segments: list
    timeline: dict
    transcript: dict = {}
    stages: dict = {}
    analysis_id: str = ""
    invocation_refs: list = []
    judge_results: list = []
    findings: list = []
    report_md: str = ""
    reason: Optional[str] = None
    audio_url: str = ""
    profile: dict = {}
    # Speaker clustering (speaker_0/speaker_1) and its evidence scope. Roles stay
    # separate: attribution carries tester/device/unknown and is never inferred
    # from speaker order or count.
    speaker_segments: list = []
    diarization_scope: dict = {}
    attribution: dict = {}


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION)


@app.post("/api/analyze", response_model=AnalysisResponse)
async def analyze(
    file: UploadFile = File(...),
    device: Optional[str] = Form(None),
    hardware: Optional[str] = Form(None),
    firmware: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    supplier: Optional[str] = Form(None),
    environment: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    frame_ms: float = Form(30.0),
    hop_ms: float = Form(10.0),
    min_speech_ms: float = Form(100.0),
    min_silence_ms: float = Form(200.0),
    timeout_ms: float = Form(5000.0),
    false_endpoint_ms: float = Form(300.0),
) -> AnalysisResponse:
    """Import supported recordings without overwriting original evidence."""
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in ('.wav', '.mp3', '.m4a'):
        raise HTTPException(400, '请选择 WAV、MP3 或 M4A 录音')
    profile = {key: (value.strip() or None) if value is not None else None for key, value in dict(device=device, hardware=hardware,
        firmware=firmware, model=model, prompt=prompt, supplier=supplier,
        environment=environment, notes=notes).items()}
    if any(value and len(value) > 4000 for value in profile.values()):
        raise HTTPException(400, '设备信息字段不能超过 4000 字符')
    # Validate detector configuration before allocating durable artifacts.
    try:
        EnergyVadSegmenter(frame_ms=frame_ms, hop_ms=hop_ms,
            min_speech_ms=min_speech_ms, min_silence_ms=min_silence_ms)
    except ValueError:
        raise HTTPException(400, '无效的音频分段参数') from None
    snapshot, providers = _model_settings().capture()
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / ('upload' + suffix)
        size = 0
        with source.open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_INPUT_BYTES:
                    raise HTTPException(413, '录音不能超过 1 GB')
                target.write(chunk)
        run_dir, manifest = await run_in_threadpool(import_recording, source, OUTPUT_ROOT,
            profile=profile, providers=providers, model_snapshot=snapshot)
    return AnalysisResponse(**_load_run(run_dir))


def _read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _run_dir(run_id):
    import re
    if not re.fullmatch(r'RUN-[A-Za-z0-9_-]+', run_id):
        raise HTTPException(404, 'Run not found')
    path = (OUTPUT_ROOT / run_id).resolve()
    if not path.is_relative_to(OUTPUT_ROOT.resolve()) or not path.is_dir():
        raise HTTPException(404, 'Run not found')
    return path


def _load_run(directory):
    manifest = _read(directory / 'manifest.json')
    unified = manifest.get('workflow') == 'recording_import' and not (directory / 'web-analysis').exists()
    root = directory / 'analysis' / manifest['analysis_id'] if unified else (
        directory / 'web-analysis' if (directory / 'web-analysis').is_dir() else directory)
    if not root.resolve().is_relative_to(directory.resolve()):
        raise HTTPException(404, 'Invalid analysis path')
    report = _read(root / 'report.json')
    manifest = _read(directory / 'manifest.json')
    timeline = _read(root / 'timeline.json') or report.get('timeline', {})
    status_doc = _read(directory / 'web-status.json')
    status = manifest.get('status') if unified else (status_doc.get('status') or report.get('run_summary', {}).get('status')
              or timeline.get('status') or manifest.get('status') or 'partial')
    def items(name, key, fallback):
        doc = _read(root / (name + '.json')) or report.get(fallback, {})
        if isinstance(doc, dict) and unified:
            doc = doc.get('data') or {}
        return doc if isinstance(doc, list) else doc.get(key, [])
    def document(name):
        doc = _read(root / (name + '.json'))
        if isinstance(doc, dict) and unified:
            return doc.get('data') or {}
        return doc if isinstance(doc, dict) else {}
    if unified:
        timeline = timeline.get('data') or {}
    diarization_doc = document('speaker-assignments')
    result = dict(transcript=(_read(root / 'transcript.json').get('data') or {}) if unified else {},
        stages=manifest.get('stages', {}), analysis_id=manifest.get('analysis_id', ''),
        invocation_refs=[a for a in manifest.get('artifacts', []) if a['kind']=='provider_invocation'],
        run_id=directory.name, status=status, reason=status_doc.get('reason'),
        profile=_read(root / 'profile.json') or manifest.get('profile', {}),
        fused_segments=items('fused-segments', 'segments', 'fused_segments'),
        acoustic_segments=items('acoustic-segments', 'segments', 'acoustic_segments'),
        speaker_segments=diarization_doc.get('speaker_segments', []),
        diarization_scope=diarization_doc.get('scope', {}),
        attribution=document('attribution'),
        turns=items('turns', 'turns', 'turns'), events=timeline.get('events', []),
        timeline=timeline, metrics=items('metrics', 'metrics', 'metrics'),
        judge_results=items('judge-results', 'results', 'judge_results'),
        findings=items('findings', 'findings', 'findings'),
        report_md=(root / 'report.md').read_text(encoding='utf-8') if (root / 'report.md').exists() else '',
        audio_url='/api/runs/' + directory.name + '/audio')
    return result


@app.get('/api/runs')
def list_runs():
    runs = []
    for entry in OUTPUT_ROOT.iterdir():
        if entry.is_dir() and entry.name.startswith('RUN-') and not entry.is_symlink():
            data = _load_run(entry)
            runs.append(dict(run_id=entry.name, status=data['status'],
                device=data['profile'].get('device'), created=entry.stat().st_mtime))
    return {'runs': sorted(runs, key=lambda r: r['created'], reverse=True)}


@app.get('/api/runs/{run_id}')
def get_run(run_id: str):
    return _load_run(_run_dir(run_id))


@app.post('/api/runs/{run_id}/resume', response_model=AnalysisResponse)
async def resume_run(run_id: str, request: Request):
    # Explicit retry: a previous timed-out billable request may have succeeded.
    body = await request.json()
    if not isinstance(body, dict) or body.get('retry_asr') is not True:
        raise HTTPException(400, '重试可能再次调用云服务，请明确提交 retry_asr=true')
    directory = _run_dir(run_id)
    if (directory / 'web-analysis').exists():
        raise HTTPException(409, '旧版分析请重新导入；原记录保持不变')
    snapshot, providers = _model_settings().capture()
    def retry():
        from .run_lock import run_lock
        with run_lock(directory):
            resume_recording(directory, providers=providers, model_snapshot=snapshot)
    try:
        await run_in_threadpool(retry)
    except Exception:
        raise HTTPException(409, '无法重试：请检查 ASR 配置、证据完整性或是否已有分析正在执行') from None
    return AnalysisResponse(**_load_run(directory))


@app.get('/api/runs/{run_id}/audio')
def get_audio(run_id: str):
    directory = _run_dir(run_id)
    manifest = _read(directory / 'manifest.json')
    artifact = next((a for a in manifest.get('artifacts', []) if a['kind'] == 'normalized_audio'), None)
    path = (directory / artifact['path']).resolve() if artifact else directory / 'normalized.wav'
    if not path.is_relative_to(directory) or not path.is_file():
        raise HTTPException(404, 'Audio unavailable')
    return FileResponse(path, media_type='audio/wav')



def _model_settings():
    from .model_settings import ModelSettings
    return ModelSettings(OUTPUT_ROOT / '.model-settings')


@app.get('/api/models')
def model_settings():
    return _model_settings().describe()


@app.post('/api/models')
async def update_models(request: Request):
    from urllib.parse import urlsplit
    from .model_settings import SettingsError, RevisionConflict
    origin = request.headers.get('origin')
    if origin and (urlsplit(origin).netloc != request.url.netloc or urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, '请从当前服务页面修改模型配置')
    if len(await request.body()) > 128 * 1024:
        raise HTTPException(413, '配置请求过大')
    try:
        payload = await request.json()
        return _model_settings().update(payload)
    except RevisionConflict as error:
        raise HTTPException(409, str(error)) from None
    except (SettingsError, ValueError):
        # Validation errors must never echo write-only credentials or arbitrary input.
        raise HTTPException(400, '配置无效，请检查服务地址、用途、参数和默认模型') from None


# ---------------------------------------------------------------------------
# Active Voice Test (PRD-F020/F021/F023)
# ---------------------------------------------------------------------------

from .voice_test import VoiceTestManager, PHASE_OBSERVED

_voice_test_manager = VoiceTestManager(OUTPUT_ROOT)


@app.post('/api/voice-test/sessions')
async def create_voice_test_session(request: Request):
    """Create a voice test session (fixed or free mode)."""
    body = await request.json()
    mode = body.get('mode', 'fixed')
    if mode not in ('fixed', 'free'):
        raise HTTPException(400, 'mode must be "fixed" or "free"')
    try:
        session = _voice_test_manager.create_session(
            mode,
            phrases=body.get('phrases'),
            device=body.get('device'),
            goal=body.get('goal'),
            constraints=body.get('constraints'),
            max_turns=body.get('max_turns', 10),
            llm_model=body.get('llm_model'),
            on_no_response=body.get('on_no_response', 'pause'),
            no_response_timeout_ms=body.get('no_response_timeout_ms'),
            capture_mode=body.get('capture_mode', 'auto'),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    return session.to_dict()


@app.get('/api/voice-test/sessions')
def list_voice_test_sessions():
    return {'sessions': [s.to_dict() for s in _voice_test_manager.sessions.values()]}


@app.get('/api/voice-test/sessions/{session_id}')
def get_voice_test_session(session_id: str):
    session = _voice_test_manager.get_session(session_id)
    if session:
        return session.to_dict()
    # A control record outlives the in-memory session (e.g. after a restart),
    # so a finished run stays inspectable instead of disappearing.
    record = _voice_test_manager.load_record(session_id)
    if record:
        return record
    raise HTTPException(404, 'Session not found')


@app.get('/api/voice-test/sessions/{session_id}/execution-record')
def get_voice_test_execution_record(session_id: str):
    """Export the minimal control trace of a session.

    This is control state (what the platform did and what the browser
    reported), not measurement evidence.
    """
    record = _voice_test_manager.load_record(session_id)
    if not record:
        raise HTTPException(404, 'Execution record not found')
    return record


@app.post('/api/voice-test/sessions/{session_id}/synthesize')
async def synthesize_voice_test(session_id: str):
    """Generate TTS audio for all phrases (fixed mode)."""
    session = _voice_test_manager.get_session(session_id)
    if not session:
        raise HTTPException(404, 'Session not found')
    if session.mode != 'fixed':
        raise HTTPException(400, 'Synthesis is for fixed mode only')
    if session.status == 'generating':
        raise HTTPException(409, 'Synthesis is already in progress')
    session.status = 'generating'
    try:
        await run_in_threadpool(_voice_test_manager.synthesize_all, session_id)
        # Return the complete snapshot so callers retain the session identifier
        # and use the same phrase records for preview playback.
        return session.to_dict()
    except Exception as error:
        session.status = 'failed'
        raise HTTPException(502, str(error)) from None


@app.get('/api/voice-test/sessions/{session_id}/audio/{phrase_index}')
def get_voice_test_audio(session_id: str, phrase_index: int):
    """Serve generated TTS audio for playback in the browser."""
    audio_path = _voice_test_manager.get_audio_path(session_id, phrase_index)
    if not audio_path or not audio_path.is_file():
        raise HTTPException(404, 'Audio not found')
    return FileResponse(str(audio_path), media_type='audio/wav')


@app.post('/api/voice-test/sessions/{session_id}/start')
def start_voice_test(session_id: str):
    """Start a voice test session."""
    try:
        session = _voice_test_manager.start(session_id)
        if not session:
            raise HTTPException(404, 'Session not found')
        return session.to_dict()
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/voice-test/sessions/{session_id}/stop')
def stop_voice_test(session_id: str):
    """Stop a running voice test session."""
    session = _voice_test_manager.stop(session_id)
    if not session:
        raise HTTPException(404, 'Session not found')
    return session.to_dict()


@app.post('/api/voice-test/sessions/{session_id}/device-audio')
async def upload_device_audio(session_id: str, request: Request):
    """Upload captured device audio for ASR (free mode).

    The browser records the device's response and uploads it here. The backend
    runs ASR and returns the transcript, which the WebSocket loop feeds to the
    LLM agent.
    """
    session = _voice_test_manager.get_session(session_id)
    if not session:
        raise HTTPException(404, 'Session not found')
    if session.mode != 'free':
        raise HTTPException(400, 'Device audio upload is for free mode only')
    content = await request.body()
    if len(content) > 60_000_000:
        raise HTTPException(413, 'Audio too large')
    audio_path = session.directory / f'device-turn-{len(session.turns):04d}.wav'
    audio_path.write_bytes(content)
    # Run ASR on the captured audio (near-real-time, not streaming)
    try:
        transcript = await run_in_threadpool(
            _transcribe_device_audio, session_id, str(audio_path))
        return {'transcript': transcript, 'audio_path': str(audio_path.relative_to(OUTPUT_ROOT))}
    except Exception as error:
        return {'transcript': '', 'error': str(error)}


def _transcribe_device_audio(session_id, audio_path):
    """Transcribe captured device audio using the configured ASR provider."""
    from .asr import transcribe_file
    from .model_settings import ModelSettings
    settings = ModelSettings(OUTPUT_ROOT / '.model-settings')
    _, providers = settings.capture()
    if not providers or not providers.asr:
        raise RuntimeError('No ASR provider configured')
    provider = providers.asr(OUTPUT_ROOT)
    _, transcript = transcribe_file(Path(audio_path), provider, OUTPUT_ROOT / 'voice-test' / session_id,
                                    source_role='unknown')
    return transcript.get('text', '')


@app.websocket('/api/voice-test/sessions/{session_id}/ws')
async def voice_test_websocket(websocket: WebSocket, session_id: str):
    """WebSocket for real-time voice test control.

    Fixed mode: the browser plays audio, detects a *suspected* device response
    via VAD, and reports events. The server advances the phrase sequence.

    Free mode: the browser plays generated phrases, captures device audio, and
    uploads it for ASR. The server calls the LLM agent to decide the next phrase.

    Control rules enforced here (PRD-F020):
    - Only the turn currently awaiting an observation may be advanced, by an
      event that names that turn. Duplicate, late, or stale-session events are
      acknowledged as ignored and never advance the run twice.
    - A turn advances on an observed response end or, decided by the session's
      explicit ``on_no_response`` policy, on a reported no-response timeout.
      A timeout is recorded as "no response observed", never as a response.
    - Every server play instruction and every browser report is written to the
      session's control trace.
    """
    session = _voice_test_manager.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason='Session not found')
        return
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get('type')
            turn_id = message.get('turn_id')

            if msg_type == 'start':
                session = _voice_test_manager.start(session_id)
                if session.mode == 'fixed':
                    await _send_play(websocket, session, 0)
                else:
                    # Free mode answers need device speech; decide once per run
                    # whether that uses Streaming ASR or the labelled fallback.
                    resolution = _resolve_capture_mode(session)
                    await websocket.send_json({'type': 'capture_mode', **resolution})
                    await _generate_and_send_free_phrase(websocket, session)

            elif msg_type == 'capture_result':
                # The audio socket already finalised the streaming capture; the
                # control loop reads the server-side result, never the browser's
                # echo of it.
                if session.mode != 'free':
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'capture results are for free mode only')
                    continue
                result = session.last_capture_result or {}
                if not result:
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'no finished capture for this session')
                    continue
                if result.get('turn_id') != session.awaiting_turn_id:
                    await _send_ignored(websocket, msg_type, turn_id, 'stale capture result')
                    continue
                if session.status != 'running':
                    await _send_ignored(websocket, msg_type, turn_id,
                                        f'session is {session.status}')
                    continue
                await _consume_device_observation(websocket, session, result)

            elif msg_type == 'stop':
                # Idempotent: stopping an already stopped session is not an error.
                _voice_test_manager.stop(session_id, reason='user_stop')
                await websocket.send_json({'type': 'stopped', 'reason': 'user_stop'})
                break

            elif msg_type in ('playback_started', 'playback_ended',
                              'playback_cancelled', 'playback_failed'):
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if turn is None or (turn.closed and msg_type != 'playback_cancelled'):
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'unknown or closed turn')
                    continue
                if session.status != 'running':
                    await _send_ignored(websocket, msg_type, turn_id,
                                        f'session is {session.status}')
                    continue
                _voice_test_manager.note_playback(
                    session, turn, msg_type, reason=message.get('reason'))
                if msg_type == 'playback_failed':
                    reason = message.get('reason') or 'playback_failed'
                    _voice_test_manager.fail(session_id, reason=f'playback_failed:{reason}')
                    await websocket.send_json({
                        'type': 'failed', 'reason': 'playback_failed',
                        'detail': reason, 'turn_id': turn.turn_id,
                    })

            elif msg_type == 'device_speech_start':
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if not _voice_test_manager.is_current_turn(session, turn):
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'not the turn awaiting an observation')
                    continue
                _voice_test_manager.note_observation(session, turn, 'observation_speech_start')
                await websocket.send_json({'type': 'listening',
                                           'turn_id': turn.turn_id,
                                           'note': 'suspected response (browser VAD)',
                                           'phrase_index': session.current_phrase_index})

            elif msg_type == 'device_speech_end':
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if not _voice_test_manager.is_current_turn(session, turn):
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'not the turn awaiting an observation')
                    continue
                _voice_test_manager.note_observation(session, turn, 'observation_speech_end')
                if session.mode == 'fixed':
                    if not await _advance_fixed(websocket, session, turn):
                        break
                # Free mode: the browser uploads the captured audio separately
                # and then sends 'device_audio_ready'.

            elif msg_type == 'observation_timeout':
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if not _voice_test_manager.is_current_turn(session, turn):
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'not the turn awaiting an observation')
                    continue
                _voice_test_manager.record_event(
                    session, 'observation_timeout', turn_id=turn.turn_id,
                    source='browser',
                    detail={'note': 'no response observed before the control wait bound',
                            'wait_ms': message.get('wait_ms')})
                _voice_test_manager.close_turn(
                    session, turn, phase='no_response', status='no_response',
                    closure_reason='no_response_timeout',
                    observation='no_response', observation_basis=None)
                if session.on_no_response == 'continue':
                    if session.mode == 'fixed':
                        if not await _advance_fixed(websocket, session, turn,
                                                    already_closed=True):
                            break
                    else:
                        await websocket.send_json({
                            'type': 'no_response', 'turn_id': turn.turn_id,
                            'policy': 'continue',
                            'note': 'no response observed; continuing by policy'})
                else:
                    _voice_test_manager.stop(session_id, reason='no_response_timeout')
                    await websocket.send_json({
                        'type': 'stopped', 'reason': 'no_response_timeout',
                        'turn_id': turn.turn_id,
                        'note': 'no response observed before the control wait bound'})
                    break

            elif msg_type == 'device_audio_ready':
                if session.status != 'running':
                    await _send_ignored(websocket, msg_type, turn_id,
                                        f'session is {session.status}')
                    continue
                # Fallback path (capture_mode = turn_file): the browser recorded
                # the whole answer and the backend transcribed it with File ASR.
                # It is routed through the same observation handling so an empty
                # transcript is never treated as an answer, and it is labelled so
                # it can never be reported as streaming.
                device_text = message.get('transcript', '')
                await _consume_device_observation(websocket, session, {
                    'turn_id': turn_id or session.awaiting_turn_id,
                    'mode': 'turn_file',
                    'final_text': device_text,
                    'final_basis': 'file_asr_fallback',
                    'final_source': 'turn_file_file_asr',
                    'failure': None if device_text.strip() else 'asr_no_final',
                })

            else:
                await _send_ignored(websocket, msg_type, turn_id, 'unknown message type')

    except WebSocketDisconnect:
        _voice_test_manager.stop(session_id, reason='disconnected')
    except Exception as error:
        try:
            await websocket.send_json({'type': 'error', 'reason': str(error)})
        except Exception:
            pass
        _voice_test_manager.fail(session_id, reason=f'control_error:{error}')


@app.websocket('/api/voice-test/sessions/{session_id}/audio')
async def voice_test_audio_websocket(websocket: WebSocket, session_id: str):
    """Binary PCM transport for Streaming ASR (free mode).

    Separate from the JSON control socket on purpose: mixing large binary audio
    frames into the control protocol would make both harder to reason about.

    Frame contract (documented in docs/24-streaming-asr.md):
      text  : {"type": "capture_started", "turn_id", "sample_rate", "channels", "bits"}
      text  : {"type": "capture_stopped", "reason"}
      bytes : [4-byte big-endian sequence][PCM16LE audio]

    Audio is forwarded to the backend's Streaming ASR provider; the browser never
    talks to the ASR service and never holds its credential.
    """
    from .streaming_asr import StreamingASRUnavailable

    session = _voice_test_manager.get_session(session_id)
    if not session or session.mode != 'free':
        await websocket.close(code=4004, reason='Free-mode session not found')
        return
    await websocket.accept()
    capture = None
    try:
        hello = await websocket.receive_json()
        if hello.get('type') != 'capture_started':
            await websocket.send_json({'type': 'capture_error',
                                       'error': 'stream_open_failed',
                                       'note': 'the first audio frame must be capture_started'})
            await websocket.close(code=4005, reason='capture_started expected')
            return
        sample_rate = hello.get('sample_rate')
        channels = hello.get('channels')
        bits = hello.get('bits')
        turn_id = hello.get('turn_id')
        if (sample_rate, channels, bits) != (16000, 1, 16):
            # Never stream audio in a format the recogniser was not configured for.
            await websocket.send_json({'type': 'capture_error', 'error': 'invalid_audio',
                                       'note': 'streaming ASR requires 16 kHz mono PCM16',
                                       'received': {'sample_rate': sample_rate,
                                                    'channels': channels, 'bits': bits}})
            await websocket.close(code=4006, reason='unsupported audio format')
            return
        turn = _voice_test_manager.turn_by_id(session, turn_id)
        if not _voice_test_manager.is_current_turn(session, turn):
            await websocket.send_json({'type': 'capture_error', 'error': 'stream_open_failed',
                                       'note': 'no turn is awaiting an observation'})
            await websocket.close(code=4007, reason='stale turn')
            return
        if session.resolved_capture_mode != 'streaming':
            await websocket.send_json({'type': 'capture_error', 'error': 'stream_open_failed',
                                       'note': 'this run is not using streaming capture'})
            await websocket.close(code=4008, reason='streaming capture not selected')
            return
        if session.capture is not None and session.capture.status == 'active':
            await websocket.send_json({'type': 'capture_error', 'error': 'stream_open_failed',
                                       'note': 'a capture is already active for this run'})
            await websocket.close(code=4009, reason='capture already active')
            return
        factory = _streaming_asr_factory()
        if factory is None:
            await websocket.send_json({'type': 'capture_error',
                                       'error': 'stream_open_failed',
                                       'note': 'streaming ASR is not configured'})
            await websocket.close(code=4010, reason='streaming ASR unavailable')
            return
        try:
            asr = await factory(session.directory).start_session(
                session_id=session_id, turn_id=turn_id, run_index=session.run_index,
                directory=session.directory / 'streaming')
        except Exception:  # noqa: BLE001 - one explicit category to the browser
            await websocket.send_json({'type': 'capture_error',
                                       'error': 'stream_open_failed',
                                       'note': 'the streaming ASR session could not be opened'})
            await websocket.close(code=4011, reason='ASR session failed')
            return
        # Starting a provider session awaits network I/O. Re-check ownership
        # afterwards so a stop or a competing socket cannot steal this turn.
        turn = _voice_test_manager.turn_by_id(session, turn_id)
        if (not _voice_test_manager.is_current_turn(session, turn)
                or (session.capture is not None and session.capture.status == 'active')):
            await asr.cancel(reason='stale_or_competing_capture')
            await websocket.send_json({'type': 'capture_error', 'error': 'stream_open_failed',
                                       'note': 'the turn changed while ASR was opening'})
            await websocket.close(code=4012, reason='capture ownership changed')
            return
        capture = _voice_test_manager.begin_capture(
            session, turn_id=turn_id, stream_id=asr.stream_id, sample_rate=sample_rate,
            channels=channels, bits=bits, mode='streaming', asr=asr)
        await websocket.send_json({'type': 'capture_ready', 'stream_id': asr.stream_id,
                                   'turn_id': turn_id})

        while True:
            message = await websocket.receive()
            if message.get('type') == 'websocket.disconnect':
                await _abort_capture(session, capture, failure='stream_disconnected')
                return
            turn = _voice_test_manager.turn_by_id(session, turn_id)
            if (not _voice_test_manager.is_current_turn(session, turn)
                    or session.capture is not capture):
                await _abort_capture(session, capture, failure='stream_disconnected')
                try:
                    await websocket.send_json({'type': 'capture_error',
                                               'error': 'stream_disconnected',
                                               'note': 'the run or turn is no longer active'})
                except Exception:
                    pass
                return
            if message.get('bytes') is not None:
                payload = message['bytes']
                if len(payload) < 4:
                    await _abort_capture(session, capture, failure='invalid_audio')
                    await websocket.send_json({'type': 'capture_error',
                                               'error': 'invalid_audio'})
                    break
                sequence = int.from_bytes(payload[:4], 'big')
                pcm = payload[4:]
                if not _apply_frame_ordering(capture, sequence):
                    # WebSocket itself is ordered. A duplicate/late sequence is
                    # therefore stale client data and must not be replayed into
                    # the recogniser, though the trace still accounts for it.
                    continue
                try:
                    await capture.asr.push_audio(pcm)
                except Exception:  # noqa: BLE001 - the session already recorded why
                    pass
                capture.audio_bytes += len(pcm)
                applied = _voice_test_manager.note_streaming_events(
                    session, capture.asr.poll_events(), capture=capture)
                await _forward_streaming_events(websocket, applied)
                if capture.failure in ('invalid_audio', 'audio_capture_failed'):
                    break
                continue
            text = message.get('text')
            if text is None:
                continue
            try:
                control = json.loads(text)
            except ValueError:
                continue
            if control.get('type') == 'capture_stopped':
                status, failure = await _finalise_capture(session, capture,
                                                          reason=control.get('reason'))
                await websocket.send_json({'type': 'capture_result', 'turn_id': turn_id,
                                           'stream_id': capture.stream_id,
                                           'final_text': capture.final_text,
                                           'final_basis': capture.final_basis,
                                           'failure': capture.failure, 'status': status,
                                           'empty_transcript': not capture.final_text.strip()})
                return
    except WebSocketDisconnect:
        await _abort_capture(session, capture, failure='stream_disconnected')
    except Exception:  # noqa: BLE001 - never leave the run hanging
        await _abort_capture(session, capture, failure='provider_error')
        try:
            await websocket.send_json({'type': 'capture_error', 'error': 'provider_error',
                                       'note': 'the streaming capture ended unexpectedly'})
        except Exception:
            pass


async def _forward_streaming_events(websocket, events):
    """Show the device's words to the operator while the turn is still running.

    Partial text is display-only: it never triggers the agent, and the trace
    keeps it labelled as a control observation.
    """
    for event in events:
        if event.kind not in ('partial_transcript', 'final_transcript'):
            continue
        if not event.text:
            continue
        try:
            await websocket.send_json({
                'type': event.kind, 'text': event.text, 'basis': event.basis,
                'sequence': event.sequence, 'source': event.source,
                'evidence_scope': 'control_evidence'})
        except Exception:  # noqa: BLE001 - a display update must not break the run
            return


def _apply_frame_ordering(capture, sequence):
    """Count frame ordering and return whether this frame may reach ASR."""
    if sequence == capture.expected_sequence:
        capture.expected_sequence += 1
        capture.last_sequence_seen = sequence
        return True
    if sequence < capture.expected_sequence:
        if capture.last_sequence_seen == sequence:
            capture.duplicate_frames += 1
        else:
            capture.late_frames += 1
        capture.last_sequence_seen = sequence
        return False
    else:
        capture.gap_frames += sequence - capture.expected_sequence
        capture.expected_sequence = sequence + 1
    capture.last_sequence_seen = sequence
    return True


async def _finalise_capture(session, capture, *, reason=None):
    """Finish the ASR input and wait, bounded, for the provider's last package."""
    from .streaming_asr import StreamingASRUnavailable
    asr = capture.asr
    status, failure = 'finished', None
    try:
        await asr.finish_input()
        deadline = time.monotonic() + (session.no_response_timeout_ms / 1000)
        while not asr.saw_last_package and time.monotonic() < deadline:
            events = await asr.wait_events(0.25)
            _voice_test_manager.note_streaming_events(session, events, capture=capture)
        _voice_test_manager.note_streaming_events(session, asr.poll_events(), capture=capture)
        if not asr.saw_last_package:
            failure = capture.failure or 'stream_timeout'
    except StreamingASRUnavailable:
        failure = capture.failure or 'stream_open_failed'
    except Exception:  # noqa: BLE001
        failure = capture.failure or 'provider_error'
    finally:
        try:
            await asr.close(reason=reason or 'client_finished')
        except Exception:  # noqa: BLE001 - cleanup failure remains a failed capture
            failure = failure or 'stream_disconnected'
        _voice_test_manager.note_streaming_events(
            session, asr.poll_events(), capture=capture)
        failure = failure or capture.failure or getattr(asr, 'failure', None)
    if failure:
        status = 'failed'
    finished = _voice_test_manager.finish_capture(
        session, capture=capture, status=status, failure=failure)
    if finished is not None:
        session.last_capture_result = finished.to_dict()
    return status, failure


async def _abort_capture(session, capture, *, failure):
    """Cancel one owned provider session and finalize its trace exactly once."""
    if capture is None or capture.status != 'active':
        return None
    asr = capture.asr
    if asr is not None:
        try:
            await asr.cancel(reason=failure)
        except Exception:  # noqa: BLE001 - retain the original terminal reason
            pass
        try:
            _voice_test_manager.note_streaming_events(
                session, asr.poll_events(), capture=capture)
        except Exception:  # noqa: BLE001 - cleanup must still release the capture
            pass
    finished = _voice_test_manager.finish_capture(
        session, capture=capture, status='cancelled', failure=failure)
    if finished is not None:
        session.last_capture_result = finished.to_dict()
    return finished


async def _send_ignored(websocket, msg_type, turn_id, reason):
    """Tell the browser an event was not applied, so it can stop waiting."""
    await websocket.send_json({
        'type': 'ignored', 'event': msg_type, 'turn_id': turn_id, 'reason': reason,
    })


async def _advance_fixed(websocket, session, turn, *, already_closed=False):
    """Close the current fixed-mode turn and send the next play instruction.

    Returns False when the run is finished (the caller must stop reading).
    """
    if not already_closed:
        _voice_test_manager.close_turn(
            session, turn, phase=PHASE_OBSERVED, status='complete',
            closure_reason='observation_speech_end',
            observation='speech_end', observation_basis='browser_vad_rms')
    next_index = turn.turn_index + 1
    if next_index < len(session.phrases):
        session.current_phrase_index = next_index
        await _send_play(websocket, session, next_index)
        return True
    _voice_test_manager.complete(session.session_id, 'all_phrases_done')
    await websocket.send_json({'type': 'complete', 'reason': 'all_phrases_done'})
    return False


def _streaming_asr_factory():
    """Resolve the configured streaming ASR provider factory, if any."""
    from .model_settings import ModelSettings
    settings = ModelSettings(OUTPUT_ROOT / '.model-settings')
    _, providers = settings.capture()
    return getattr(providers, 'streaming_asr', None)


def _resolve_capture_mode(session):
    """Decide, once per run, how device speech becomes text (PRD-F021).

    Returns the message the browser receives. A fallback is always named: the UI
    must never present the turn-file path as streaming.
    """
    if session.mode != 'free':
        session.resolved_capture_mode = None
        return {'mode': None, 'fallback_reason': None}
    factory = _streaming_asr_factory()
    available = factory is not None
    reason = None
    if session.capture_mode == 'streaming' and not available:
        mode = 'turn_file'
        reason = 'streaming_asr_not_configured'
    elif session.capture_mode == 'streaming':
        mode = 'streaming'
    elif session.capture_mode == 'turn_file':
        mode = 'turn_file'
        reason = 'configured_turn_file'
    else:  # auto
        mode = 'streaming' if available else 'turn_file'
        if not available:
            reason = 'streaming_asr_not_configured'
    session.resolved_capture_mode = mode
    session.capture_fallback_reason = reason
    _voice_test_manager.record_event(session, 'capture_mode_resolved', detail={
        'requested': session.capture_mode, 'resolved': mode,
        'streaming_available': available, 'fallback_reason': reason,
        'note': 'turn_file is a fallback path; it is not streaming ASR'})
    return {'mode': mode, 'requested': session.capture_mode,
            'fallback_reason': reason, 'streaming_available': available}


async def _consume_device_observation(websocket, session, result):
    """Turn a finished capture into the next free-mode turn.

    An empty transcript is never treated as an answer: it is recorded as "no
    observation" and the session's explicit on_no_response policy decides what
    happens next.
    """
    transcript = (result.get('final_text') or '').strip()
    turn_id = result.get('turn_id')
    failure = result.get('failure')
    _voice_test_manager.record_event(session, 'device_observation', turn_id=turn_id,
                                     detail={'capture_mode': result.get('mode'),
                                             'final_basis': result.get('final_basis'),
                                             'final_source': result.get('final_source'),
                                             'text': transcript, 'failure': failure,
                                             'evidence_scope': 'control_evidence'})
    if not transcript:
        session.status = 'stopped' if session.on_no_response == 'pause' else session.status
        reason = f'no_device_transcript:{failure}' if failure else 'no_device_transcript'
        if session.on_no_response == 'pause':
            _voice_test_manager.stop(session.session_id, reason=reason)
            await websocket.send_json({'type': 'stopped', 'reason': reason,
                                       'turn_id': turn_id,
                                       'note': 'no usable device transcript was observed'})
            return
        await websocket.send_json({'type': 'no_response', 'turn_id': turn_id,
                                   'policy': 'continue', 'failure': failure,
                                   'note': 'no usable device transcript; continuing by policy'})
        await _generate_and_send_free_phrase(websocket, session, device_text='',
                                             capture=result)
        return
    await _generate_and_send_free_phrase(websocket, session, device_text=transcript,
                                         capture=result)


async def _send_play(websocket, session, phrase_index):
    """Record and send a play instruction for a fixed-mode phrase."""
    phrase = session.phrases[phrase_index]
    audio_url = f'/api/voice-test/sessions/{session.session_id}/audio/{phrase_index}'
    turn = _voice_test_manager.open_turn(
        session, text=phrase['text'], audio_path=phrase.get('audio_path'),
        audio_sha256=phrase.get('audio_sha256'), audio_url=audio_url)
    session.current_phrase_index = phrase_index
    await websocket.send_json({
        'type': 'play',
        'turn_id': turn.turn_id,
        'phrase_index': phrase_index,
        'text': phrase['text'],
        'audio_url': audio_url,
        'playback_start_timeout_ms': 10000,
        'playback_max_duration_ms': 180000,
        'no_response_timeout_ms': session.no_response_timeout_ms,
    })


async def _generate_and_send_free_phrase(websocket, session, device_text='', capture=None):
    """Generate the next test phrase via the LLM agent (free mode).

    The agent receives the test goal, conversation history, and the device's
    latest response. It decides what to say next, or whether to stop.

    ``device_text`` is the *control* observation of what the device said; it is
    recorded with its capture provenance so a streaming transcript is never
    presented as a measurement.
    """
    from .voice_agent import generate_next_phrase
    history = [{'role': t.role, 'text': t.text} for t in session.turns if t.text]
    # Reserve the platform turn's index up front: the device's reply is recorded
    # first so the conversation reads question -> answer -> question, and the
    # spoken asset must still be named for the turn it belongs to.
    platform_index = len(session.turns) + (1 if device_text else 0)
    try:
        result = await run_in_threadpool(
            generate_next_phrase, session, history, device_text,
            OUTPUT_ROOT, _voice_test_manager, platform_index)
        if result is None:
            _voice_test_manager.complete(session.session_id, 'agent_stop')
            await websocket.send_json({'type': 'complete', 'reason': 'agent_stop'})
            return
        text, audio_path = result
        if device_text:
            _voice_test_manager.note_device_response(session, device_text)
        turn = _voice_test_manager.open_turn(
            session, text=text, audio_path=audio_path,
            audio_url=f'/api/voice-test/sessions/{session.session_id}/audio/{platform_index}')
        audio_url = turn.audio_url
        await websocket.send_json({
            'type': 'play',
            'turn_id': turn.turn_id,
            'phrase_index': turn.turn_index,
            'text': text,
            'audio_url': audio_url,
            'device_text': device_text,
            'observation_scope': 'control_evidence' if device_text else None,
            'capture': {
                'mode': session.resolved_capture_mode,
                'fallback_reason': session.capture_fallback_reason,
                'sample_rate': 16000, 'channels': 1, 'bits': 16,
            } if session.resolved_capture_mode else None,
            'playback_start_timeout_ms': 10000,
            'playback_max_duration_ms': 180000,
            'no_response_timeout_ms': session.no_response_timeout_ms,
        })
        platform_turns = sum(1 for t in session.turns if t.role == 'platform')
        if platform_turns >= session.max_turns:
            _voice_test_manager.complete(session.session_id, 'max_turns')
            await websocket.send_json({'type': 'complete', 'reason': 'max_turns'})
    except Exception as error:
        await websocket.send_json({'type': 'error', 'reason': str(error)})
        _voice_test_manager.fail(session.session_id, reason=f'agent_error:{error}')
