"""Simple Web API for the AIVoiceBench analysis pipeline.

Accepts a WAV file upload, runs acoustic segmentation → fusion → metrics,
and returns structured JSON results. Non-canonical WAV (non-16kHz/mono) is
converted in-process; MP3/M4A requires external FFmpeg (mount or configure
FFMPEG_PATH).

Endpoints:
  GET  /health         — health check
  POST /api/analyze    — upload WAV, run full pipeline, return results
  GET  /api/runs       — list previous analysis runs from output directory
"""

from __future__ import annotations

from array import array
import json
import math
import os
import sys
import tempfile
import uuid
import wave
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .acoustic import EnergyVadSegmenter, segment_audio
from .fusion import fuse, build_turns, detect_events, generate_timeline
from .metrics import compute_timeline_metrics
from .runner import write_json

app = FastAPI(
    title="AIVoiceBench",
    description="AI Voice Terminal Evaluation — Recording Import & Analysis API",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_ROOT = Path(os.environ.get("AIVOICEBENCH_OUTPUT", "artifacts"))
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)


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


@app.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok", version="0.1.0")


def _normalize_wav(source: Path, output: Path) -> Path:
    """Convert any WAV to PCM16 16kHz mono using only Python stdlib.

    Handles: stereo→mono downmix, sample rate conversion (linear interpolation),
    bit depth conversion. Does NOT handle MP3/M4A (needs FFmpeg).
    """
    with wave.open(str(source), "rb") as inp:
        channels = inp.getnchannels()
        sample_width = inp.getsampwidth()
        rate = inp.getframerate()
        frames = inp.getnframes()
        raw = inp.readframes(frames)

    if sample_width != 2:
        # Convert to 16-bit
        raise HTTPException(
            status_code=400,
            detail=f"WAV sample width must be 16-bit (got {sample_width * 8}-bit). "
                   "Use FFmpeg to pre-convert, or mount FFmpeg and set FFMPEG_PATH.",
        )

    samples = array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()

    # Stereo → mono (equal weight downmix)
    if channels == 2:
        mono = array("h", [0] * (len(samples) // 2))
        for i in range(len(mono)):
            left = samples[i * 2]
            right = samples[i * 2 + 1]
            val = (left + right) // 2
            mono[i] = max(-32768, min(32767, val))
        samples = mono
    elif channels > 2:
        raise HTTPException(
            status_code=400,
            detail=f"Multi-channel ({channels}) not supported. Export mono or stereo first.",
        )

    # Resample to 16kHz (linear interpolation)
    target_rate = 16000
    if rate != target_rate:
        ratio = target_rate / rate
        n_out = int(len(samples) * ratio)
        resampled = array("h", [0] * n_out)
        for i in range(n_out):
            src_pos = i / ratio
            src_idx = int(src_pos)
            frac = src_pos - src_idx
            if src_idx + 1 < len(samples):
                s1 = samples[src_idx]
                s2 = samples[src_idx + 1]
                val = int(s1 + (s2 - s1) * frac)
            else:
                val = samples[src_idx] if src_idx < len(samples) else 0
            resampled[i] = max(-32768, min(32767, val))
        samples = resampled

    # Write canonical WAV
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(target_rate)
        data = array("h", samples)
        if sys.byteorder != "little":
            data.byteswap()
        out.writeframes(data.tobytes())

    return output


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
    """Upload a WAV file and run the full analysis pipeline."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".wav",):
        raise HTTPException(
            status_code=400,
            detail=f"Only WAV is supported directly. For MP3/M4A, pre-convert with FFmpeg "
                   f"or mount FFmpeg and set FFMPEG_PATH. Got: {suffix}",
        )

    run_id = "RUN-" + uuid.uuid4().hex[:12]
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded file
    raw_path = run_dir / f"source{suffix}"
    content = await file.read()
    raw_path.write_bytes(content)

    # Normalize to canonical WAV
    canonical_path = run_dir / "normalized.wav"
    try:
        _normalize_wav(raw_path, canonical_path)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Normalization failed: {exc}")

    # Run acoustic segmentation
    segmenter = EnergyVadSegmenter(
        frame_ms=frame_ms, hop_ms=hop_ms,
        min_speech_ms=min_speech_ms, min_silence_ms=min_silence_ms,
    )
    acoustic_result = segmenter.segment(canonical_path)
    acoustic_doc = acoustic_result.to_dict()
    write_json(run_dir / "acoustic-segments.json", acoustic_doc)

    # Run fusion → turns → events → timeline
    fused_doc = fuse(acoustic_doc)
    turns_doc = build_turns(fused_doc)
    events, evidence, status, reason = detect_events(
        fused_doc, turns_doc, timeout_ms=timeout_ms, false_endpoint_ms=false_endpoint_ms)
    timeline_doc = generate_timeline(fused_doc, turns_doc, events, evidence, status, reason)
    write_json(run_dir / "fused-segments.json", fused_doc)
    write_json(run_dir / "turns.json", turns_doc)
    write_json(run_dir / "timeline.json", timeline_doc)

    # Run metrics
    metrics_result = compute_timeline_metrics(timeline_doc)
    write_json(run_dir / "metrics.json", metrics_result)

    # Save profile
    profile = {
        "device": device, "hardware": hardware, "firmware": firmware,
        "model": model, "prompt": prompt, "supplier": supplier,
        "environment": environment, "notes": notes,
    }
    write_json(run_dir / "profile.json", profile)

    return AnalysisResponse(
        run_id=run_id,
        status=timeline_doc["status"],
        fused_segments=fused_doc["segments"],
        turns=turns_doc["turns"],
        events=timeline_doc["events"],
        metrics=metrics_result["metrics"],
        acoustic_segments=acoustic_doc["segments"],
        timeline=timeline_doc,
    )


@app.get("/api/runs")
def list_runs() -> JSONResponse:
    """List previous analysis runs from the output directory."""
    runs = []
    if OUTPUT_ROOT.exists():
        for entry in sorted(OUTPUT_ROOT.iterdir(), reverse=True):
            if entry.is_dir() and entry.name.startswith("RUN-"):
                manifest = entry / "timeline.json"
                profile_path = entry / "profile.json"
                profile = {}
                if profile_path.exists():
                    profile = json.loads(profile_path.read_text(encoding="utf-8"))
                status = "unknown"
                if manifest.exists():
                    tl = json.loads(manifest.read_text(encoding="utf-8"))
                    status = tl.get("status", "unknown")
                runs.append({
                    "run_id": entry.name,
                    "status": status,
                    "device": profile.get("device"),
                    "created": entry.stat().st_mtime,
                })
    return JSONResponse({"runs": runs})


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> JSONResponse:
    """Get details of a specific analysis run."""
    run_dir = OUTPUT_ROOT / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    result = {"run_id": run_id}
    for name in ("acoustic-segments", "fused-segments", "turns", "timeline", "metrics", "profile"):
        path = run_dir / f"{name}.json"
        if path.exists():
            result[name] = json.loads(path.read_text(encoding="utf-8"))
    return JSONResponse(result)
