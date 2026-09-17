"""Versioned file-audio processing; never opens a recording or playback device."""

from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Protocol
import wave

from .runner import digest, write_json

MAX_RECORDING_MS = 30 * 60 * 1000
MAX_INPUT_BYTES = 1024 * 1024 * 1024
FORMATS = {'.wav': 'wav', '.mp3': 'mp3', '.m4a': 'mov'}


class AudioProcessingError(ValueError):
    """A locally authored safe failure reason; native diagnostics stay in local logs."""


@dataclass
class NormalizedAudio:
    path: Path
    metadata: dict
    processor: dict
    artifacts: list[Path]


class AudioProcessingProvider(Protocol):
    def normalize(self, source: Path, output: Path) -> NormalizedAudio: ...


def _command(argv, directory, name, timeout):
    record = {'argv': argv, 'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'timeout_seconds': timeout, 'shell': False}
    start = time.monotonic()
    stdout, stderr = directory / f'{name}.stdout.txt', directory / f'{name}.stderr.txt'
    try:
        with stdout.open('xb') as out, stderr.open('xb') as err:
            result = subprocess.run(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                timeout=timeout, shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        record.update(returncode=result.returncode, status='complete' if result.returncode == 0 else 'failed')
        if result.returncode != 0:
            raise AudioProcessingError(f'{name} failed; native diagnostics are retained in the Run')
    except subprocess.TimeoutExpired:
        record['status'] = 'failed'
        record['reason'] = 'timeout'
        raise AudioProcessingError(f'{name} exceeded its processing timeout') from None
    except OSError:
        record['status'] = 'failed'
        record['reason'] = 'process_or_storage_unavailable'
        raise AudioProcessingError(f'{name} could not run or save its outputs') from None
    finally:
        record['latency_ms'] = round((time.monotonic() - start) * 1000, 3)
        write_json(directory / f'{name}.invocation.json', record)
    return stdout


def qa_conditions(all_silent):
    """Report validity conditions derived from real measurements, without inventing a threshold.

    Every condition states only what was measured and what it does *not* prove. No
    acceptance threshold is configured for this milestone, so no condition may be
    reported as pass/fail and no condition may claim recognition quality, ASR
    accuracy or measurement accuracy. A condition that cannot support downstream
    analysis is `insufficient`, never silently `met`.
    """
    return [
        {'condition_id': 'decodable_canonical_audio', 'status': 'met',
         'basis': 'ffmpeg decode produced canonical WAV PCM16LE 16000 Hz mono samples',
         'limitation': 'Decoding success does not prove the recording contains speech'},
        {'condition_id': 'nonempty_signal', 'status': 'insufficient' if all_silent else 'unassessed',
         'basis': 'peak/rms over the decoded canonical samples' if all_silent
                  else 'no energy threshold is configured, so signal sufficiency is not assessed',
         'limitation': 'Signal presence is a measurement; it is not a quality, accuracy or acceptance result'},
    ]


def canonical_qa(path, max_duration_ms=MAX_RECORDING_MS):
    """Streaming measurements: no whole-recording buffer or perceptual pass claim."""
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise AudioProcessingError('Canonical audio must be WAV PCM16LE 16000 Hz mono')
        frames = audio.getnframes()
        if not 0 < frames <= 16 * max_duration_ms:
            raise AudioProcessingError('Decoded audio is empty or exceeds the 30-minute import limit')
        count = peak = total = squared = clipped = 0
        while raw := audio.readframes(65536):
            if len(raw) % 2:
                raise AudioProcessingError('Truncated canonical audio')
            values = array('h', raw)
            if sys.byteorder != 'little':
                values.byteswap()
            count += len(values)
            peak = max(peak, max(abs(value) for value in values))
            total += sum(values)
            squared += sum(value * value for value in values)
            clipped += sum(value in (-32768, 32767) for value in values)
        if count != frames:
            raise AudioProcessingError('Truncated canonical WAV sample data')
    return {'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE', 'container': 'wav',
            'sample_count': frames, 'duration_ms': frames / 16, 'peak': peak / 32768,
            'rms': math.sqrt(squared / frames) / 32768, 'dc_offset': total / frames / 32768,
            'clipped_samples': clipped, 'all_silent': squared == 0,
            'conditions': qa_conditions(squared == 0),
            'timing_basis': 'canonical_audio_samples'}


class FFmpegAudioProcessor:
    """Explicit FFmpeg adapter; cloud normalization can implement the same interface."""
    def __init__(self, ffmpeg=None, ffprobe=None):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def _tools(self, output):
        tools = {}
        for name, configured in [('ffmpeg', self.ffmpeg), ('ffprobe', self.ffprobe)]:
            executable = shutil.which(str(configured or name))
            if not executable:
                raise AudioProcessingError(f'{name} is unavailable; configure its local executable path')
            executable = str(Path(executable).resolve())
            version_path = _command([executable, '-version'], output, name + '-version', 10)
            text = version_path.read_text(encoding='utf-8', errors='replace').splitlines()
            tools[name] = {'executable': executable, 'sha256': digest(executable), 'version': text[0] if text else 'unknown'}
        return tools

    def normalize(self, source, output):
        source, output = Path(source).resolve(), Path(output).resolve()
        output.mkdir(parents=True, exist_ok=False)
        if source.suffix.lower() not in FORMATS:
            raise AudioProcessingError('Supported import formats are WAV, MP3 and M4A')
        tools = self._tools(output)
        # Force the intended demuxer and disable network protocols and MOV external data references.
        input_options = ['-protocol_whitelist', 'file,pipe', '-f', FORMATS[source.suffix.lower()]]
        if source.suffix.lower() == '.m4a':
            input_options += ['-enable_drefs', '0']
        probe_path = _command([tools['ffprobe']['executable'], '-v', 'error', *input_options,
            '-show_format', '-show_streams', '-of', 'json', str(source)], output, 'probe', 30)
        try:
            native = json.loads(probe_path.read_text(encoding='utf-8'))
            streams = [s for s in native['streams'] if s.get('codec_type') == 'audio']
            if len(streams) != 1:
                raise AudioProcessingError('Import requires exactly one audio stream; select/export the intended stream first')
            stream = streams[0]
            channels = int(stream['channels'])
            if channels not in (1, 2):
                raise AudioProcessingError('Initial import supports mono/stereo sources; multichannel needs an explicit mapping')
            duration = float(stream.get('duration', native.get('format', {}).get('duration', 'nan')))
            if not math.isfinite(duration) or not 0 < duration <= MAX_RECORDING_MS / 1000:
                raise AudioProcessingError('Source duration is unknown, empty or exceeds the 30-minute limit')
            rate = int(stream['sample_rate'])
            if not 8000 <= rate <= 192000:
                raise AudioProcessingError('Source sample rate is outside supported bounds')
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, AudioProcessingError):
                raise
            raise AudioProcessingError('Invalid audio metadata from probe') from None
        # Explicit equal-weight stereo downmix; no gain normalization, denoising or silence trimming.
        filters = (['pan=mono|c0=0.5*c0+0.5*c1'] if channels == 2 else []) + [
            'aresample=16000:resampler=swr:filter_size=32:phase_shift=10:dither_method=none']
        partial = output / 'normalized.incomplete.wav'
        _command([tools['ffmpeg']['executable'], '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-xerror', '-err_detect', 'explode', *input_options, '-i', str(source), '-map', '0:a:0',
            '-vn', '-sn', '-dn', '-map_metadata', '-1', '-af', ','.join(filters), '-ar', '16000',
            '-ac', '1', '-c:a', 'pcm_s16le', '-threads', '1', '-bitexact', '-n', str(partial)],
            output, 'convert', 180)
        qa = canonical_qa(partial)
        # Compressed duration includes padding; preserve differences rather than silently correcting them.
        difference = qa['duration_ms'] - duration * 1000
        if source.suffix.lower() == '.wav' and abs(difference) > 2:
            raise AudioProcessingError('Decoded WAV duration disagrees with source metadata; possible truncation')
        normalized = output / 'normalized.wav'
        partial.rename(normalized)
        processor = {'provider': 'local_ffmpeg', 'processor_version': '1.0.0', 'model': None,
                     'tools': tools, 'config': {'filter': filters, 'audio_stream': '0:a:0',
                     'target_format': 'WAV_PCM16LE_16000_mono', 'gain_normalization': False,
                     'silence_trimming': False, 'max_duration_ms': MAX_RECORDING_MS,
                     'network_protocols_enabled': False}}
        metadata = {'schema_version': '1.0.0', 'source': {'codec': stream.get('codec_name'),
                    'container': native.get('format', {}).get('format_name'), 'sample_rate_hz': rate,
                    'channels': channels, 'duration_ms': duration * 1000,
                    'stream_start_time_seconds': stream.get('start_time'),
                    'format_start_time_seconds': native.get('format', {}).get('start_time')},
                    'normalized': qa, 'duration_difference_ms': difference,
                    'mapping': {'canonical_origin': 'first retained decoded sample', 'unit': 'ms',
                        'canonical_sample_period_ms': 0.0625, 'source_alignment_status': 'needs_review',
                        'source_offset_ms': None, 'source_alignment_uncertainty_ms': None,
                        'note': 'Decoder handles codec padding/edit lists; source-to-canonical acoustic alignment is not calibrated'},
                    'speaker_isolation': 'not_performed', 'processor': processor}
        write_json(output / 'audio-metadata.json', metadata)
        return NormalizedAudio(normalized, metadata, processor, sorted(output.iterdir()))
