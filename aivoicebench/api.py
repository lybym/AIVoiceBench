"""Web recording import, recoverable analysis, history and audio playback."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .acoustic import EnergyVadSegmenter
from .runner import write_json, digest
from .version import VERSION
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
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
    if unified:
        timeline = timeline.get('data') or {}
    result = dict(transcript=(_read(root / 'transcript.json').get('data') or {}) if unified else {},
        stages=manifest.get('stages', {}), analysis_id=manifest.get('analysis_id', ''),
        invocation_refs=[a for a in manifest.get('artifacts', []) if a['kind']=='provider_invocation'],
        run_id=directory.name, status=status, reason=status_doc.get('reason'),
        profile=_read(root / 'profile.json') or manifest.get('profile', {}),
        fused_segments=items('fused-segments', 'segments', 'fused_segments'),
        acoustic_segments=items('acoustic-segments', 'segments', 'acoustic_segments'),
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
