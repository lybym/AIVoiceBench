"""Web recording import, recoverable analysis, history and audio playback."""

from __future__ import annotations

import json
import os
import tempfile
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

from .voice_test import VoiceTestManager, VoiceTestTurn

_voice_test_manager = VoiceTestManager(OUTPUT_ROOT)


@app.post('/api/voice-test/sessions')
async def create_voice_test_session(request: Request):
    """Create a voice test session (fixed or free mode)."""
    body = await request.json()
    mode = body.get('mode', 'fixed')
    if mode not in ('fixed', 'free'):
        raise HTTPException(400, 'mode must be "fixed" or "free"')
    session = _voice_test_manager.create_session(
        mode,
        phrases=body.get('phrases'),
        device=body.get('device'),
        goal=body.get('goal'),
        constraints=body.get('constraints'),
        max_turns=body.get('max_turns', 10),
        llm_model=body.get('llm_model'),
    )
    return session.to_dict()


@app.get('/api/voice-test/sessions')
def list_voice_test_sessions():
    return {'sessions': [s.to_dict() for s in _voice_test_manager.sessions.values()]}


@app.get('/api/voice-test/sessions/{session_id}')
def get_voice_test_session(session_id: str):
    session = _voice_test_manager.get_session(session_id)
    if not session:
        raise HTTPException(404, 'Session not found')
    return session.to_dict()


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

    Fixed mode: the browser plays audio, detects device speech via VAD, and
    reports events. The server advances the phrase sequence.

    Free mode: the browser plays generated phrases, captures device audio, and
    uploads it for ASR. The server calls the LLM agent to decide the next phrase.
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

            if msg_type == 'start':
                session = _voice_test_manager.start(session_id)
                if session.mode == 'fixed':
                    # Send the first phrase to play
                    await _send_play(websocket, session, 0)
                else:
                    # Free mode: generate the first phrase via LLM
                    await _generate_and_send_free_phrase(websocket, session)

            elif msg_type == 'stop':
                _voice_test_manager.stop(session_id, reason='user_stop')
                await websocket.send_json({'type': 'stopped', 'reason': 'user_stop'})
                break

            elif msg_type == 'device_speech_start':
                await websocket.send_json({'type': 'listening',
                                           'phrase_index': session.current_phrase_index})

            elif msg_type == 'device_speech_end':
                if session.mode == 'fixed':
                    next_index = session.current_phrase_index + 1
                    if next_index < len(session.phrases):
                        session.current_phrase_index = next_index
                        await _send_play(websocket, session, next_index)
                    else:
                        session.status = 'completed'
                        session.stop_reason = 'all_phrases_done'
                        await websocket.send_json({'type': 'complete',
                                                   'reason': 'all_phrases_done'})
                        break
                else:
                    # Free mode: the browser will upload audio separately,
                    # then send 'device_audio_ready'. Nothing to do here yet.
                    pass

            elif msg_type == 'device_audio_ready':
                # Free mode: ASR has been run (via the POST endpoint), and the
                # browser reports the transcript. The server calls the LLM agent.
                device_text = message.get('transcript', '')
                await _generate_and_send_free_phrase(websocket, session, device_text)

    except WebSocketDisconnect:
        _voice_test_manager.stop(session_id, reason='disconnected')
    except Exception as error:
        await websocket.send_json({'type': 'error', 'reason': str(error)})
        _voice_test_manager.stop(session_id, reason='error')


async def _send_play(websocket, session, phrase_index):
    """Send a play command for a fixed-mode phrase."""
    phrase = session.phrases[phrase_index]
    await websocket.send_json({
        'type': 'play',
        'phrase_index': phrase_index,
        'text': phrase['text'],
        'audio_url': f'/api/voice-test/sessions/{session.session_id}/audio/{phrase_index}',
    })


async def _generate_and_send_free_phrase(websocket, session, device_text=''):
    """Generate the next test phrase via the LLM agent (free mode).

    The agent receives the test goal, conversation history, and the device's
    latest response. It decides what to say next, or whether to stop.
    """
    from .voice_agent import generate_next_phrase
    history = [{'role': t.role, 'text': t.text} for t in session.turns]
    try:
        result = await run_in_threadpool(
            generate_next_phrase, session, history, device_text,
            OUTPUT_ROOT, _voice_test_manager)
        if result is None:
            # Agent decided to stop
            session.status = 'completed'
            session.stop_reason = 'agent_stop'
            await websocket.send_json({'type': 'complete', 'reason': 'agent_stop'})
            return
        text, audio_path = result
        turn_index = len(session.turns)
        session.turns.append(VoiceTestTurn(
            turn_index=turn_index, role='platform', text=text,
            audio_path=audio_path, started_at=utc_now_iso(),
            status='playing'))
        if device_text:
            session.turns.append(VoiceTestTurn(
                turn_index=turn_index, role='device', text=device_text,
                observation='speech_end', started_at=utc_now_iso(),
                status='observed'))
        await websocket.send_json({
            'type': 'play',
            'phrase_index': turn_index,
            'text': text,
            'audio_url': f'/api/voice-test/sessions/{session.session_id}/audio/{turn_index}',
            'device_text': device_text,
        })
        if len(session.turns) >= session.max_turns * 2:
            session.status = 'completed'
            session.stop_reason = 'max_turns'
            await websocket.send_json({'type': 'complete', 'reason': 'max_turns'})
    except Exception as error:
        await websocket.send_json({'type': 'error', 'reason': str(error)})
        _voice_test_manager.stop(session.session_id, reason='error')


def utc_now_iso():
    from datetime import timezone
    from datetime import datetime
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
