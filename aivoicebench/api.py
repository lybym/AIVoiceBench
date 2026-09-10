"""Web recording import, recoverable analysis, history and audio playback."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .acoustic import EnergyVadSegmenter
from .runner import write_json
from .version import VERSION
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from .import_pipeline import import_recording
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
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / ('upload' + suffix)
        size = 0
        with source.open('wb') as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_INPUT_BYTES:
                    raise HTTPException(413, '录音不能超过 1 GB')
                target.write(chunk)
        run_dir, manifest = await run_in_threadpool(import_recording, source, OUTPUT_ROOT, profile=profile)
    normalized = next((a for a in manifest['artifacts'] if a['kind'] == 'normalized_audio'), None)
    if normalized:
        from .pipeline import run_full_pipeline
        try:
            await run_in_threadpool(run_full_pipeline, run_dir / normalized['path'],
                run_dir / 'web-analysis', profile, frame_ms=frame_ms, hop_ms=hop_ms,
                min_speech_ms=min_speech_ms, min_silence_ms=min_silence_ms,
                timeout_ms=timeout_ms, false_endpoint_ms=false_endpoint_ms)
        except Exception:
            write_json(run_dir / 'web-status.json', {'status': 'failed',
                'reason': '分析未完成，原始录音及导入证据已保留'})
    else:
        write_json(run_dir / 'web-status.json', {'status': 'failed',
            'reason': '音频标准化失败，请检查文件格式或转换工具；导入证据已保留'})
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
    root = directory / 'web-analysis' if (directory / 'web-analysis').is_dir() else directory
    report = _read(root / 'report.json')
    manifest = _read(directory / 'manifest.json')
    timeline = _read(root / 'timeline.json') or report.get('timeline', {})
    status_doc = _read(directory / 'web-status.json')
    status = (status_doc.get('status') or report.get('run_summary', {}).get('status')
              or timeline.get('status') or manifest.get('status') or 'partial')
    def items(name, key, fallback):
        doc = _read(root / (name + '.json')) or report.get(fallback, {})
        return doc if isinstance(doc, list) else doc.get(key, [])
    result = dict(run_id=directory.name, status=status, reason=status_doc.get('reason'),
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


@app.get('/api/runs/{run_id}/audio')
def get_audio(run_id: str):
    directory = _run_dir(run_id)
    manifest = _read(directory / 'manifest.json')
    artifact = next((a for a in manifest.get('artifacts', []) if a['kind'] == 'normalized_audio'), None)
    path = (directory / artifact['path']).resolve() if artifact else directory / 'normalized.wav'
    if not path.is_relative_to(directory) or not path.is_file():
        raise HTTPException(404, 'Audio unavailable')
    return FileResponse(path, media_type='audio/wav')
