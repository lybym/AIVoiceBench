"""Vendor boundary and timestamped external ASR. Never device-internal truth."""

from array import array
from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import shutil
import sys
from typing import Protocol
import uuid
import wave

from .runner import digest, write_json
from .validation import transcript_errors


@dataclass
class ProviderOutput:
    profile: dict
    raw_messages: list[str]


class ASRProvider(Protocol):
    def transcribe(self, mono_wav: Path) -> ProviderOutput:
        """Return native messages and the actually selected model/config profile."""
        ...

    def normalize(self, messages: list[str], duration_ms: float) -> tuple[list, list]:
        """Map native messages to the shared segment/gap contract."""
        ...


def model_fingerprint(model_dir):
    root = Path(model_dir).resolve()
    if not root.is_dir():
        raise ValueError('ASR model directory does not exist')
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Model directory cannot contain external links')
        if path.is_file():
            files.append({'path': path.relative_to(root).as_posix(), 'sha256': digest(path)})
    if not files:
        raise ValueError('ASR model directory is empty')
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class VoskProvider:
    def __init__(self, model_dir, model_version):
        if not isinstance(model_version, str) or not model_version.strip():
            raise ValueError('Explicit provider model version is required')
        try:
            import vosk
        except (ImportError, OSError) as error:
            raise ValueError('Install requirements-asr.txt for the optional local Vosk adapter') from error
        model_dir = Path(model_dir)
        fingerprint = model_fingerprint(model_dir)
        vosk.SetLogLevel(-1)
        try:
            self.model = vosk.Model(str(model_dir))
        except Exception as error:
            raise ValueError('Cannot load the selected Vosk model') from error
        self.vosk = vosk
        self.profile = {'provider': 'vosk', 'library_version': version('vosk'), 'model_id': model_dir.name,
                        'model_version': model_version, 'model_sha256': fingerprint,
                        'config': {'words': True, 'sample_rate_hz': 16000, 'chunk_frames': 4000}}

    def transcribe(self, mono_wav):
        messages = []
        with wave.open(str(mono_wav), 'rb') as audio:
            recognizer = self.vosk.KaldiRecognizer(self.model, audio.getframerate())
            recognizer.SetWords(True)
            while data := audio.readframes(4000):
                if recognizer.AcceptWaveform(data):
                    messages.append(recognizer.Result())
            messages.append(recognizer.FinalResult())
        return ProviderOutput(self.profile, messages)

    def normalize(self, messages, duration_ms):
        return normalize_vosk(messages, duration_ms)


def prepare_channel(source, destination, channel=None, max_duration_ms=600000):
    if type(max_duration_ms) is not int or not 1 <= max_duration_ms <= 1800000:
        raise ValueError('ASR duration limit must be within 1–1800000 ms')
    with wave.open(str(source), 'rb') as audio:
        channels = audio.getnchannels()
        if audio.getsampwidth() != 2 or audio.getframerate() != 16000 or audio.getcomptype() != 'NONE' or channels not in (1, 2):
            raise ValueError('ASR requires 16 kHz PCM16 WAV, mono or explicitly selected stereo channel')
        if channel is None:
            if channels != 1:
                raise ValueError('Stereo input requires explicit --channel 1 or 2; no automatic speaker inference')
            channel = 1
        if type(channel) is not int or not 1 <= channel <= channels:
            raise ValueError('Requested channel is outside source audio')
        frames = audio.getnframes()
        if not 0 < frames <= 16 * max_duration_ms:
            raise ValueError('ASR audio is empty or exceeds the configured duration limit')
        raw = audio.readframes(frames)
        if len(raw) != frames * channels * 2:
            raise ValueError('Truncated source WAV')
    samples = array('h', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    selected = samples[channel - 1::channels]
    if sys.byteorder != 'little':
        selected.byteswap()
    with wave.open(str(destination), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(selected.tobytes())
    return frames / 16, channel


def normalize_vosk(messages, duration_ms):
    """Preserve provider timing; lexical confidence is not timestamp confidence."""
    segments = []
    gaps = []
    previous_end = 0
    for raw_index, raw in enumerate(messages):
        message = json.loads(raw)
        if not isinstance(message, dict) or not isinstance(message.get('text', ''), str):
            raise ValueError('Invalid provider message shape')
        text = message.get('text', '').strip()
        words = message.get('result', [])
        if not isinstance(words, list) or any(not isinstance(word, dict) for word in words):
            raise ValueError('Provider words must be an array of objects')
        if not words:
            if text:
                gaps.append({'raw_message_index': raw_index, 'reason': 'Provider returned text without timestamped words', 'text': text})
            continue
        normalized = []
        for word in words:
            start, end, confidence = word.get('start'), word.get('end'), word.get('conf')
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in (start, end, confidence)):
                raise ValueError('Provider word timing/confidence is missing or nonfinite')
            start, end = start * 1000, end * 1000
            if start < previous_end - 0.001 or end < start or end > duration_ms + 0.001 or not 0 <= confidence <= 1:
                raise ValueError('Provider word timing/confidence is out of bounds or order')
            if not isinstance(word.get('word'), str) or not word['word'].strip():
                raise ValueError('Provider word text is empty')
            normalized.append({'text': word['word'], 'start_ms': start, 'end_ms': end, 'recognition_confidence': confidence})
            previous_end = end
        segments.append({'segment_id': f'ASR-{raw_index + 1:04d}', 'text': text or ' '.join(word['text'] for word in normalized),
                         'start_ms': normalized[0]['start_ms'], 'end_ms': normalized[-1]['end_ms'],
                         'timestamp_source': 'asr_provider', 'timestamp_confidence': None,
                         'speaker_id': None, 'raw_message_index': raw_index, 'words': normalized})
    return segments, gaps


def transcribe_file(source, provider, output_root, source_role='unknown', channel=None, run_id=None, case_id=None,
                    max_duration_ms=600000):
    if source_role not in ('stimulus', 'device_output', 'room_mix', 'unknown'):
        raise ValueError('Unknown source role')
    if (run_id is None) != (case_id is None):
        raise ValueError('run_id and case_id must be provided together')
    output = Path(output_root) / ('ASR-' + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    try:
        # Keep a local source snapshot so future references do not depend on changed inputs.
        shutil.copyfile(source, output / 'source.wav')
        duration, selected_channel = prepare_channel(output / 'source.wav', output / 'input-mono.wav', channel, max_duration_ms)
        result = provider.transcribe(output / 'input-mono.wav')
        raw_path = output / 'raw-provider.json'
        # Native strings preserved exactly, including fields unused by normalization.
        write_json(raw_path, {'messages': result.raw_messages})
        segments, gaps = provider.normalize(result.raw_messages, duration)
        transcript = {'schema_version': getattr(provider, 'transcript_version', '1.0.0'), 'transcript_id': output.name, 'run_id': run_id, 'case_id': case_id,
                      'measurement_scope': 'external_asr', 'time_base': 'audio_relative_ms',
                      'time_mapping': {'status': 'unmapped', 'offset_ms': None, 'uncertainty_ms': None},
                      'source': {'path': 'source.wav', 'sha256': digest(output / 'source.wav'), 'role': source_role,
                                 'selected_channel': selected_channel, 'duration_ms': duration,
                                 'provider_input_path': 'input-mono.wav', 'provider_input_sha256': digest(output / 'input-mono.wav')},
                      'provider_profile': result.profile, 'raw_response': {'path': raw_path.name, 'sha256': digest(raw_path)},
                      'status': 'partial' if gaps else 'complete', 'gaps': gaps, 'segments': segments}
        errors = transcript_errors(transcript)
        if errors:
            raise ValueError('Invalid normalized transcript: ' + '\n'.join(errors))
        write_json(output / 'transcript.json', transcript)
        return output, transcript
    except Exception as error:
        write_json(output / 'error.json', {'status': 'blocked', 'reason': 'ASR processing failed', 'error_type': type(error).__name__,
                                         'note': 'No successful normalized transcript is claimed; retained local inputs/raw response if available'})
        raise
