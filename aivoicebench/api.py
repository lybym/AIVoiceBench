"""Web recording import, recoverable analysis, history and audio playback."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
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
from .run_view import (RunViewError, list_run_views, project_workbench, read_json,
                       read_run_view)

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
    # Acoustic↔ASR-speaker-span alignment: the deterministic overlap/coverage facts
    # behind every assignment or abstention, including unmatched duration, per-cluster
    # coverage and the low-energy distribution. It never maps a cluster to a role.
    alignment: dict = {}
    # Why role-dependent metrics are unavailable, with counts. Present so an empty
    # metric list is explainable instead of looking like dropped UI data.
    metrics_gap: dict = {}
    # Audio QA for the canonical artifact: measurements plus validity conditions and
    # the envelope's own evidence status. It never carries a recognition, accuracy or
    # acceptance claim. Empty when the Run never produced QA.
    audio_qa: dict = {}
    # Manual speaker-role review gate: the anonymous clusters, the evidence needed to
    # decide each one, the saved human revision and the revision diff. Role-dependent
    # stages stay blocked until every cluster has an explicit user decision.
    role_review: dict = {}
    # Reviewer-facing projection of the current AnalysisRevision for the wavesurfer
    # Evidence Workbench (PRD-F013/F014). Every region coordinate is a persisted
    # evidence interval; the browser never becomes a measurement producer.
    workbench: dict = {}


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
    return read_json(path)


def _run_dir(run_id):
    import re
    if not re.fullmatch(r'RUN-[A-Za-z0-9_-]+', run_id):
        raise HTTPException(404, 'Run not found')
    path = (OUTPUT_ROOT / run_id).resolve()
    if not path.is_relative_to(OUTPUT_ROOT.resolve()) or not path.is_dir():
        raise HTTPException(404, 'Run not found')
    return path


def _workbench(directory, *, tolerant=True):
    """Web adapter for the shared Workbench projection (see :mod:`aivoicebench.run_view`)."""
    return project_workbench(directory, tolerant=tolerant)


def _load_run(directory, *, include_workbench=True):
    """Web adapter for the shared Run view.

    The projection itself is surface-neutral (`aivoicebench.run_view`), so the API,
    the CLI and any automation read the same Run/revision/status semantics. This
    adapter only translates "no view can exist for this directory" into the HTTP
    status the endpoints already promise.
    """
    try:
        return read_run_view(directory, include_workbench=include_workbench)
    except RunViewError as error:
        raise HTTPException(404, str(error)) from None


@app.get('/api/runs')
def list_runs():
    """The Run listing, produced by the same projection one Run's detail view uses.

    A separate listing implementation would be a second source of truth for
    `status`/revision, which is exactly what Issue #27 forbids.
    """
    return {'runs': list_run_views(OUTPUT_ROOT, include_workbench=False)}


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


@app.get('/api/runs/{run_id}/role-review')
def get_role_review(run_id: str):
    """Speaker clusters, the evidence needed to decide them, and the gate state."""
    directory = _run_dir(run_id)
    view = _load_run(directory).get('role_review') or {}
    if not view:
        raise HTTPException(404, '该记录没有可复核的说话人聚类')
    return view


@app.post('/api/runs/{run_id}/role-review', response_model=AnalysisResponse)
async def save_role_review(run_id: str, request: Request):
    """Save one explicit human decision per cluster and rerun role-dependent stages.

    Every cluster must be decided, including a deliberate `unknown`. Saving creates a
    new immutable AnalysisRevision; the previous revision's artifacts are untouched.
    """
    from urllib.parse import urlsplit
    origin = request.headers.get('origin')
    if origin and (urlsplit(origin).netloc != request.url.netloc
                   or urlsplit(origin).scheme != request.url.scheme):
        raise HTTPException(403, '请从当前服务页面提交人工角色确认')
    if len(await request.body()) > 64 * 1024:
        raise HTTPException(413, '角色确认请求过大')
    directory = _run_dir(run_id)
    if (directory / 'web-analysis').exists():
        raise HTTPException(409, '旧版分析请重新导入；原记录保持不变')
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, '请求内容无效') from None
    if not isinstance(payload, dict):
        raise HTTPException(400, '请求内容无效')
    mapping = payload.get('mapping')
    reviewer = payload.get('reviewer')
    reason = payload.get('reason') or ''
    if not isinstance(mapping, dict) or not mapping:
        raise HTTPException(400, '请为每个说话人聚类提交明确角色（tester/device/unknown）')
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise HTTPException(400, '请填写复核人身份')
    if not isinstance(reason, str):
        raise HTTPException(400, '复核说明必须是文本')
    snapshot, _providers = _model_settings().capture()
    from .import_pipeline import apply_role_mapping
    from .role_review import RoleReviewError
    from .run_lock import run_lock

    def apply():
        with run_lock(directory):
            apply_role_mapping(directory, mapping, reviewer, reason=reason,
                               model_snapshot=snapshot, providers=_providers)

    try:
        await run_in_threadpool(apply)
    except RoleReviewError as error:
        # Input problems state the real reason; they never echo credentials.
        raise HTTPException(400, str(error)) from None
    except ValueError:
        raise HTTPException(409, '无法应用角色确认：该记录的可用证据不足，请先完成转写与说话人聚类') from None
    except Exception:
        raise HTTPException(409, '无法应用角色确认：请检查证据完整性或是否已有分析正在执行') from None
    return AnalysisResponse(**_load_run(directory))


@app.get('/api/runs/{run_id}/evidence-workbench')
def get_evidence_workbench(run_id: str):
    """Regions, tracks and provenance for the wavesurfer Evidence Workbench.

    Returned as its own document so CLI/automation can verify the exact evidence
    geometry the browser is allowed to draw, independently of the page.

    Three states are kept distinct on purpose: an absent Run/Revision is 404; a
    projection that fails is 500, because the evidence exists and the reviewer must not
    be told it does not; a projection that succeeds carries its own gaps in
    ``unavailable``/``evidence_integrity``.
    """
    directory = _run_dir(run_id)
    try:
        document = _workbench(directory, tolerant=False)
    except (OSError, ValueError) as error:
        raise HTTPException(
            500, f'证据工作台投影失败（该 Run 的证据存在但无法投影）：{error}') from None
    if not document:
        raise HTTPException(404, '该记录没有可复核的证据工作台')
    return document


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
        result = _model_settings().update(payload)
        # A new configuration must be visible to the next voice-test precheck and
        # run, not only to the next analysis.
        _voice_test_manager.invalidate_providers()
        return result
    except RevisionConflict as error:
        raise HTTPException(409, str(error)) from None
    except (SettingsError, ValueError):
        # Validation errors must never echo write-only credentials or arbitrary input.
        raise HTTPException(400, '配置无效，请检查服务地址、用途、参数和默认模型') from None


# ---------------------------------------------------------------------------
# Active Voice Test (PRD-F020/F021/F023)
# ---------------------------------------------------------------------------

from .voice_test import VoiceTestManager, PHASE_OBSERVED, PHASE_FAILED, CapabilityError

_voice_test_manager = VoiceTestManager(OUTPUT_ROOT)


@app.get('/api/voice-test/capabilities/{mode}')
def voice_test_capabilities(mode: str, capture_mode: str | None = None,
                            session_id: str | None = None):
    """What this mode needs, and what is missing (PRD-F021 precheck).

    Presence and adapter readiness only: no paid probe is issued, so this is not
    a connectivity claim, and the response never contains credentials, service
    addresses or signed URLs. The same check is enforced at ``start`` — this
    endpoint only lets the page refuse a run before it raises the microphone.
    """
    if mode not in ('fixed', 'free'):
        raise HTTPException(400, 'Unknown voice-test mode')
    session = _voice_test_manager.get_session(session_id) if session_id else None
    if session_id and session is None:
        raise HTTPException(404, 'Session not found')
    try:
        return _voice_test_manager.capability_report(
            mode, requested_capture_mode=capture_mode, session=session)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


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
            round_observation_max_ms=body.get('round_observation_max_ms'),
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
    """Serve generated TTS audio for playback in the browser.

    The Content-Type follows the stored container. Active TTS is fixed to MP3
    (Issue #98), so advertising ``audio/wav`` for an MP3 stimulus would make a
    browser that trusts the header pick the wrong decoder.
    """
    audio_path = _voice_test_manager.get_audio_path(session_id, phrase_index)
    if not audio_path or not audio_path.is_file():
        raise HTTPException(404, 'Audio not found')
    from .voice_test import tts_media_type_for_path
    return FileResponse(str(audio_path), media_type=tts_media_type_for_path(audio_path))


@app.post('/api/voice-test/sessions/{session_id}/start')
def start_voice_test(session_id: str):
    """Start a voice test session.

    The per-mode capability precheck is enforced by the manager, so this endpoint
    and the control socket cannot bypass it. A refused start names what is
    missing and makes no provider call.
    """
    try:
        session = _voice_test_manager.start(session_id)
        if not session:
            raise HTTPException(404, 'Session not found')
        return session.to_dict()
    except CapabilityError as error:
        raise HTTPException(409, {'reason': 'capability_precheck_failed',
                                  'mode': error.mode,
                                  'missing': error.report.get('missing', []),
                                  'missing_names': error.report.get('missing_names', []),
                                  'connectivity': error.report.get('connectivity'),
                                  'note': '未发起模型或语音调用；缺少项必须补齐，或显式选择降级路径'}) from None
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
    """Upload a whole-turn recording for File ASR (free-mode labelled fallback).

    The browser records the device's whole answer and uploads the container it
    actually produced. The server decodes and converts it to canonical 16 kHz
    mono PCM16 WAV before File ASR, extracts the transcript from the normalized
    segments, and records an explicit failure state when the media is invalid,
    recognition fails, or no segment carries text. A failure is never returned as
    a successful empty transcript, and the browser's own transcript payload is
    never trusted: only the server-side capture record can advance a turn.
    """
    session = _voice_test_manager.get_session(session_id)
    if not session:
        raise HTTPException(404, 'Session not found')
    if session.mode != 'free':
        raise HTTPException(400, 'Device audio upload is for free mode only')
    capture_id = request.headers.get('x-voice-capture-id', '')
    turn_id = request.headers.get('x-voice-turn-id', '')
    declared = request.headers.get('x-voice-mime-type') or request.headers.get('content-type', '')
    mime_type = declared.split(';', 1)[0].strip().lower()
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', capture_id):
        raise HTTPException(400, 'Invalid capture identity')
    extension = {'audio/webm': '.webm', 'audio/ogg': '.ogg', 'audio/mp4': '.m4a',
                 'audio/wav': '.wav', 'audio/wave': '.wav'}.get(mime_type)
    if not extension:
        raise HTTPException(415, 'Unsupported captured-audio MIME type')
    try:
        capture = _voice_test_manager.begin_upload(session, capture_id, turn_id, mime_type)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if capture.get('status') == 'recognized':
        return _capture_response(capture)
    content = await request.body()
    if not 512 <= len(content) <= 60_000_000:
        _voice_test_manager.finish_upload(session, capture_id, status='invalid_audio',
                                          reason='capture_size_out_of_range')
        raise HTTPException(413, 'Captured audio size is out of range')
    if not _looks_like_audio(content, extension):
        _voice_test_manager.finish_upload(session, capture_id, status='invalid_audio',
                                          reason='content_signature_mismatch')
        raise HTTPException(422, 'Captured audio does not match its declared format')
    audio_path = session.directory / f'capture-{capture_id}{extension}'
    audio_path.write_bytes(content)
    try:
        result = await run_in_threadpool(
            _transcribe_device_audio, session_id, str(audio_path))
    except Exception as error:
        _voice_test_manager.finish_upload(session, capture_id, status='asr_failed',
                                          reason=type(error).__name__)
        raise HTTPException(502, 'File ASR did not produce a valid transcript') from None
    if not session.is_running or not _voice_test_manager.is_current_turn(
            session, _voice_test_manager.turn_by_id(session, turn_id)):
        _voice_test_manager.finish_upload(session, capture_id, status='discarded',
                                          reason='session_stopped_or_stale')
        raise HTTPException(409, 'Capture completed after the active turn changed')
    capture = _voice_test_manager.finish_upload(
        session, capture_id, status='recognized', audio_path=str(audio_path.relative_to(OUTPUT_ROOT)),
        mime_type=mime_type, size_bytes=len(content), **result)
    return _capture_response(capture)


def _capture_response(capture):
    """The upload's server-side outcome. No credential or signed URL is included."""
    return {'capture_id': capture['capture_id'], 'turn_id': capture['turn_id'],
            'status': capture['status'], 'transcript_status': capture.get('transcript_status'),
            'asr_reference': capture.get('asr_reference'), 'audio_qa': capture.get('audio_qa')}


# The container signatures the browser's MediaRecorder can produce. This is a
# cheap format check, not a decode: ffmpeg decides whether the media is real.
AUDIO_SIGNATURES = {
    '.webm': lambda content: content.startswith(b'\x1a\x45\xdf\xa3'),
    '.ogg': lambda content: content.startswith(b'OggS'),
    '.wav': lambda content: content.startswith(b'RIFF') and content[8:12] == b'WAVE',
    '.m4a': lambda content: len(content) > 12 and content[4:8] == b'ftyp',
}


def _looks_like_audio(content, extension):
    return AUDIO_SIGNATURES[extension](content)


def _transcribe_device_audio(session_id, audio_path):
    """Decode a browser recording and transcribe it with the session's File ASR.

    The browser container (webm/ogg/mp4) is not a File ASR input: it is decoded
    and converted to canonical 16 kHz mono PCM16 WAV first, checked with the same
    canonical QA used by recording import, and only then recognized. The
    transcript text comes from the normalized segments, and an empty result is an
    error rather than an empty success.
    """
    from .asr import transcribe_file
    from .audio_processing import canonical_qa
    session = _voice_test_manager.get_session(session_id)
    if session is None:
        raise RuntimeError('Session not found')
    providers = session.config_providers or _voice_test_manager.get_providers()
    if not providers or not providers.asr:
        raise RuntimeError('No File ASR provider configured')
    source = Path(audio_path)
    decoded = source.with_suffix('.decoded.wav')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('FFmpeg is unavailable for browser audio decoding')
    _voice_test_manager.note_provider_call(session, 'asr')
    subprocess.run(
        [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-xerror', '-i', str(source),
         '-vn', '-sn', '-dn', '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', '-y', str(decoded)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=45, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    qa = canonical_qa(decoded, max_duration_ms=120000)
    provider = providers.asr(OUTPUT_ROOT)
    directory, transcript = transcribe_file(
        decoded, provider, OUTPUT_ROOT / 'voice-test' / session_id, source_role='unknown')
    text = ' '.join(str(segment.get('text', '')).strip()
                    for segment in transcript.get('segments', []) if segment.get('text')).strip()
    if not text:
        raise RuntimeError('File ASR returned no usable transcript text')
    return {'transcript': text, 'transcript_status': transcript.get('status'),
            'asr_reference': str(directory.relative_to(OUTPUT_ROOT)),
            'audio_qa': {'duration_ms': qa['duration_ms'], 'all_silent': qa['all_silent'],
                         'peak': qa['peak'], 'rms': qa['rms']}}


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
                try:
                    session = _voice_test_manager.start(session_id)
                except CapabilityError as error:
                    # Refuse before any provider call; the session is left as it
                    # was so the operator can fix the configuration and retry.
                    await websocket.send_json({
                        'type': 'blocked', 'reason': 'capability_precheck_failed',
                        'mode': error.mode,
                        'missing': error.report.get('missing', []),
                        'missing_names': error.report.get('missing_names', []),
                        'connectivity': error.report.get('connectivity'),
                        'note': '未发起模型或语音调用；缺少项必须补齐，或显式选择降级路径',
                    })
                    continue
                except ValueError as error:
                    await websocket.send_json({
                        'type': 'blocked', 'reason': 'start_refused',
                        'missing': [], 'missing_names': [],
                        'note': str(error),
                    })
                    continue
                if session.mode == 'fixed':
                    await _send_play(websocket, session, 0)
                else:
                    # The manager resolved (and enforced) how this run's device
                    # answers become text; report that, never a silent fallback.
                    await websocket.send_json({
                        'type': 'capture_mode',
                        'mode': session.resolved_capture_mode,
                        'requested': session.capture_mode,
                        'fallback_reason': session.capture_fallback_reason,
                        'streaming_available': session.resolved_capture_mode == 'streaming',
                    })
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
                if await _consume_device_observation(websocket, session, result):
                    break

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

            elif msg_type == 'vad_diagnostics':
                # The browser's noise-floor estimate for this round. Recorded as
                # control evidence so a turn's end decision can be explained;
                # it is not a measurement of the room.
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if _voice_test_manager.is_current_turn(session, turn):
                    _voice_test_manager.record_event(
                        session, 'vad_diagnostics', turn_id=turn.turn_id, source='browser',
                        detail=dict(message.get('detail') or {},
                                    evidence_scope='control_evidence',
                                    note='browser time-domain RMS thresholds; not a room measurement'))

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
                reason = message.get('reason') or 'no_response_observed'
                # A silent device and "speech started but its end cannot be
                # confirmed" are different control outcomes and must not be
                # recorded under the same closure reason.
                unconfirmed = reason == 'cannot_confirm_response_end'
                _voice_test_manager.record_event(
                    session, 'observation_timeout', turn_id=turn.turn_id,
                    source='browser',
                    detail={'note': ('speech was detected but its end could not be confirmed '
                                     'before the round bound' if unconfirmed else
                                     'no response observed before the control wait bound'),
                            'reason': reason,
                            'wait_ms': message.get('wait_ms')})
                _voice_test_manager.close_turn(
                    session, turn, phase='no_response', status='no_response',
                    closure_reason='observation_end_unconfirmed' if unconfirmed
                    else 'no_response_timeout',
                    observation='no_response', observation_basis=None)
                if session.on_no_response == 'continue':
                    if session.mode == 'fixed':
                        if not await _advance_fixed(websocket, session, turn,
                                                    already_closed=True):
                            break
                    else:
                        # Free mode: the browser's capture for this round is
                        # already abandoned, so the next question is generated
                        # here. A silent device must not stall the run, and a
                        # spent turn budget still ends it explicitly.
                        await websocket.send_json({
                            'type': 'no_response', 'turn_id': turn.turn_id,
                            'policy': 'continue',
                            'note': 'no response observed; continuing by policy'})
                        if await _continue_free_run(websocket, session):
                            break
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
                # The transcript is read from the server-side capture record, not
                # from the browser payload, so only a recognized upload advances.
                try:
                    capture = _voice_test_manager.consume_upload(
                        session, message.get('capture_id', ''), turn_id or session.awaiting_turn_id)
                except ValueError as error:
                    await _send_ignored(websocket, msg_type, turn_id, str(error))
                    continue
                if await _consume_device_observation(websocket, session, {
                    'turn_id': capture['turn_id'],
                    'mode': 'turn_file',
                    'final_text': capture.get('transcript', ''),
                    'final_basis': 'file_asr_fallback',
                    'final_source': 'turn_file_file_asr',
                    'failure': None,
                }):
                    break

            elif msg_type == 'device_audio_failed':
                # A failed fallback observation is not a silent "no answer": it
                # is a control failure with its own reason, and it never advances
                # the conversation.
                turn = _voice_test_manager.turn_by_id(session, turn_id)
                if not _voice_test_manager.is_current_turn(session, turn):
                    await _send_ignored(websocket, msg_type, turn_id,
                                        'not the turn awaiting an observation')
                    continue
                reason = message.get('reason') or 'device_audio_failed'
                _voice_test_manager.record_event(
                    session, 'device_audio_failed', turn_id=turn.turn_id, source='browser',
                    detail={'reason': reason, 'detail': message.get('detail'),
                            'note': 'fallback observation failed; not recorded as an answer'})
                _voice_test_manager.close_turn(
                    session, turn, phase=PHASE_FAILED, status='failed',
                    closure_reason=f'device_audio_failed:{reason}',
                    observation='no_response', observation_basis=None)
                _voice_test_manager.fail(session_id, reason=f'device_audio_failed:{reason}')
                await websocket.send_json({'type': 'failed', 'reason': 'device_audio_failed',
                                           'detail': reason, 'turn_id': turn.turn_id})
                break

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
        # The run's own provider snapshot decides: it is the set the precheck
        # admitted the run against, so a settings change mid-run cannot swap the
        # recogniser under an active conversation.
        providers = session.config_providers or _voice_test_manager.get_providers()
        factory = getattr(providers, 'streaming_asr', None) if providers else None
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
                await _forward_streaming_events(websocket, applied, turn_id=capture.turn_id)
                await _forward_stale_streaming_events(websocket, session)
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
                # Anything the provider produced after this turn was closed is
                # reported as ignored, never as this round's transcript.
                await _forward_stale_streaming_events(websocket, session)
                stale_reason = _voice_test_manager.stale_capture_reason(session, capture)
                await websocket.send_json({'type': 'capture_result', 'turn_id': turn_id,
                                           'stream_id': capture.stream_id,
                                           'final_text': capture.final_text,
                                           'final_basis': capture.final_basis,
                                           'failure': capture.failure, 'status': status,
                                           'empty_transcript': not capture.final_text.strip(),
                                           'stale': stale_reason is not None,
                                           'stale_reason': stale_reason})
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


async def _forward_streaming_events(websocket, events, *, turn_id=None):
    """Show the device's words to the operator while the turn is still running.

    Partial text is display-only: it never triggers the agent, and the trace
    keeps it labelled as a control observation. Every update names the turn it
    belongs to, so the page can refuse one that is no longer current.
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
                'turn_id': turn_id,
                'evidence_scope': 'control_evidence'})
        except Exception:  # noqa: BLE001 - a display update must not break the run
            return


async def _forward_stale_streaming_events(websocket, session):
    """Tell the page which provider output was ignored, and why.

    A transcript that arrives after its turn closed must not update the round's
    text: it is sent as an explicit ``stale_transcript`` notification carrying
    the session, the turn and the reason, so the page can show it as ignored
    instead of as a confirmed device answer.
    """
    for entry in _voice_test_manager.drain_stale_streaming_events(session):
        try:
            await websocket.send_json({
                'type': 'stale_transcript', 'kind': entry['kind'], 'text': entry['text'],
                'session_id': entry['session_id'], 'turn_id': entry['turn_id'],
                'reason': entry['reason'], 'evidence_scope': entry['evidence_scope'],
                'ignored': True, 'note': entry['note']})
        except Exception:  # noqa: BLE001 - a notification must not break the run
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


def _asr_terminated(asr):
    """True when the provider has ended the stream (no waiting is useful).

    ``terminated`` distinguishes "the provider ended the stream, with real
    evidence" from "the documented ``is_last_package`` field arrived"; either
    signal means no further result can arrive, so both are honoured and a
    provider that only implements the documented field still works.
    """
    return bool(getattr(asr, 'terminated', False)) or bool(getattr(asr, 'saw_last_package', False))


async def _finalise_capture(session, capture, *, reason=None):
    """Finish the ASR input and wait, bounded, for the provider to terminate."""
    from .streaming_asr import StreamingASRUnavailable
    asr = capture.asr
    status, failure = 'finished', None
    try:
        await asr.finish_input()
        deadline = time.monotonic() + (session.no_response_timeout_ms / 1000)
        # Stop as soon as the provider has terminated: a normal close that is
        # backed by a definite final transcript ends the stream even when the
        # documented last-package field never arrives, and must not be held open
        # until the control bound.
        while not _asr_terminated(asr) and time.monotonic() < deadline:
            events = await asr.wait_events(0.25)
            _voice_test_manager.note_streaming_events(session, events, capture=capture)
        _voice_test_manager.note_streaming_events(session, asr.poll_events(), capture=capture)
        if not _asr_terminated(asr):
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
    """Resolve the configured streaming ASR provider factory, if any.

    Kept for callers that need the settings-file view without a session (the
    capability precheck uses the manager's snapshot instead).
    """
    from .model_settings import ModelSettings
    settings = ModelSettings(OUTPUT_ROOT / '.model-settings')
    _, providers = settings.capture()
    return getattr(providers, 'streaming_asr', None)


async def _consume_device_observation(websocket, session, result):
    """Turn a finished capture into the next free-mode turn.

    An empty transcript is never treated as an answer: it is recorded as "no
    observation" and the session's explicit on_no_response policy decides what
    happens next. Either way the platform turn that was awaiting an observation
    is closed with its real outcome, so the trace never shows a turn left open
    while the conversation has already advanced.

    Returns True when the run is finished, so the caller stops reading.
    """
    transcript = (result.get('final_text') or '').strip()
    turn_id = result.get('turn_id')
    failure = result.get('failure')
    turn = _voice_test_manager.turn_by_id(session, turn_id)
    _voice_test_manager.record_event(session, 'device_observation', turn_id=turn_id,
                                     detail={'capture_mode': result.get('mode'),
                                             'final_basis': result.get('final_basis'),
                                             'final_source': result.get('final_source'),
                                             'text': transcript, 'failure': failure,
                                             'evidence_scope': 'control_evidence'})
    if not transcript:
        reason = f'no_device_transcript:{failure}' if failure else 'no_device_transcript'
        if turn is not None and not turn.closed:
            _voice_test_manager.close_turn(
                session, turn, phase='no_response', status='no_response',
                closure_reason=reason, observation=turn.observation,
                observation_basis=turn.observation_basis)
        if session.on_no_response == 'pause':
            _voice_test_manager.stop(session.session_id, reason=reason)
            await websocket.send_json({'type': 'stopped', 'reason': reason,
                                       'turn_id': turn_id,
                                       'note': 'no usable device transcript was observed'})
            return True
        await websocket.send_json({'type': 'no_response', 'turn_id': turn_id,
                                   'policy': 'continue', 'failure': failure,
                                   'note': 'no usable device transcript; continuing by policy'})
        return await _continue_free_run(websocket, session, capture=result)
    if turn is not None and not turn.closed:
        # Closed by the verified transcript, not by a VAD suspicion: the turn's
        # own recorded observation (if the browser reported one) is preserved.
        _voice_test_manager.close_turn(
            session, turn, phase=PHASE_OBSERVED, status='complete',
            closure_reason='device_transcript',
            observation=turn.observation, observation_basis=turn.observation_basis)
    return await _continue_free_run(websocket, session, device_text=transcript,
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
        'round_observation_max_ms': session.round_observation_max_ms,
    })


async def _continue_free_run(websocket, session, *, device_text='', capture=None):
    """Ask the next free-mode question, or finish a run whose budget is spent.

    ``max_turns=N`` bounds N *complete* rounds, so the Nth answer is always
    observed and closed before the run can end. Once the budget is spent the
    agent and TTS are not called again: the session completes with
    ``max_turns`` and no further play is issued.

    Returns True when the run is finished.
    """
    if _voice_test_manager.turn_budget_reached(session):
        if device_text:
            # The last answer is still recorded as this run's final device turn.
            # Rounds that do generate a further question record it in
            # ``_generate_and_send_free_phrase`` instead; here no question
            # follows, so nothing else would keep it in the trace.
            _voice_test_manager.note_device_response(session, device_text)
        _voice_test_manager.complete(session.session_id, 'max_turns')
        await websocket.send_json({'type': 'complete', 'reason': 'max_turns'})
        return True
    await _generate_and_send_free_phrase(websocket, session, device_text=device_text,
                                         capture=capture)
    return False


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
        if device_text:
            # Recorded before the stop decision so a final answer that ends the
            # conversation is still part of the trace and the history.
            _voice_test_manager.note_device_response(session, device_text)
        if result is None:
            _voice_test_manager.complete(session.session_id, 'agent_stop')
            await websocket.send_json({'type': 'complete', 'reason': 'agent_stop'})
            return
        if not session.is_running:
            # A Stop arrived while the non-cancellable model call was in flight.
            # Its late result stays provider-side evidence: never synthesize or
            # play it, and never open a turn for a stopped run.
            _voice_test_manager.record_event(
                session, 'late_agent_result_dropped',
                detail={'note': 'session stopped while the model call was in flight',
                        'evidence_scope': 'control_evidence'})
            return
        text, audio_path = result
        turn = _voice_test_manager.open_turn(
            session, text=text, audio_path=audio_path,
            audio_url=f'/api/voice-test/sessions/{session.session_id}/audio/{platform_index}')
        audio_url = turn.audio_url
        # A free-mode turn always carries the capture identity the server expects
        # back: the browser's answer audio is bound to this turn, not to whatever
        # turn happens to be current when the upload finishes.
        capture_id = 'cap-' + uuid.uuid4().hex
        await websocket.send_json({
            'type': 'play',
            'turn_id': turn.turn_id,
            'phrase_index': turn.turn_index,
            'text': text,
            'audio_url': audio_url,
            'device_text': device_text,
            'capture_id': capture_id,
            'observation_scope': 'control_evidence' if device_text else None,
            'capture': {
                'mode': session.resolved_capture_mode,
                'fallback_reason': session.capture_fallback_reason,
                'sample_rate': 16000, 'channels': 1, 'bits': 16,
            } if session.resolved_capture_mode else None,
            'playback_start_timeout_ms': 10000,
            'playback_max_duration_ms': 180000,
            'no_response_timeout_ms': session.no_response_timeout_ms,
            'round_observation_max_ms': session.round_observation_max_ms,
        })
        # The turn budget is deliberately NOT consumed here. ``max_turns`` counts
        # complete rounds, so the question just played is still answered and
        # observed; the run ends after that observation is recorded
        # (``_continue_free_run``), never by closing this turn early.
    except Exception as error:
        await websocket.send_json({'type': 'error', 'reason': str(error)})
        _voice_test_manager.fail(session.session_id, reason=f'agent_error:{error}')
