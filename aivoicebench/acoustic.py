"""Acoustic speech segmentation from raw audio signal.

This module produces speech-segment *candidates* using frame-based energy
VAD.  Acoustic timing is signal-processor timing: it is distinct from ASR
estimated timing, diarization timing, LLM-inferred semantic timing, or
manual corrected timing.  Segments carry no speaker role.

The processor uses only the Python standard library (``wave``, ``array``,
``math``); no NumPy or external model is required.  Cloud or model-based
VAD can implement the same ``AcousticSegmenter`` Protocol.

Evidence-first: every segment references its source audio and records the
frame statistics that motivated its boundary.  If the audio is silent or
cannot be parsed, the result is ``insufficient_evidence`` — never an
invented segment.
"""

from array import array
from dataclasses import dataclass, field
import math
import sys
import uuid
import wave
from pathlib import Path
from typing import Protocol

from .runner import digest, write_json

CANONICAL_SAMPLE_RATE = 16000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH = 2  # PCM16
DEFAULT_FRAME_MS = 30.0
DEFAULT_HOP_MS = 10.0
DEFAULT_THRESHOLD_FACTOR = 0.15  # fraction of the active range above noise floor
DEFAULT_MIN_SPEECH_MS = 100.0
DEFAULT_MIN_SILENCE_MS = 200.0
DEFAULT_MERGE_GAP_MS = 80.0
DEFAULT_PRE_ROLL_MS = 0.0
DEFAULT_POST_ROLL_MS = 0.0
SILENCE_FLOOR = 1e-7  # guard against log(0) in a fully silent recording

# Named acoustic sensitivity profiles. `canonical` IS the measurement policy used
# for recorded metrics. Any other profile exists only to *evaluate* whether the
# canonical threshold missed quiet device speech, and every document produced with
# one carries an explicit `is_canonical_measurement_policy: false` marker so a
# comparison run can never be mistaken for the canonical measurement.
CANONICAL_SENSITIVITY = 'canonical'
SENSITIVITY_PROFILES = {
    'canonical': {
        'threshold_factor': DEFAULT_THRESHOLD_FACTOR,
        'min_speech_ms': DEFAULT_MIN_SPEECH_MS,
        'min_silence_ms': DEFAULT_MIN_SILENCE_MS,
        'merge_gap_ms': DEFAULT_MERGE_GAP_MS,
        'pre_roll_ms': DEFAULT_PRE_ROLL_MS,
        'post_roll_ms': DEFAULT_POST_ROLL_MS,
    },
    'quiet_device': {
        # Diagnostic profile for low-volume device responses: a lower active-range
        # threshold plus roll to keep a quiet onset that the canonical threshold
        # would drop. Not a measurement policy.
        'threshold_factor': 0.05,
        'min_speech_ms': 60.0,
        'min_silence_ms': 300.0,
        'merge_gap_ms': 150.0,
        'pre_roll_ms': 100.0,
        'post_roll_ms': 150.0,
    },
}


class AcousticError(ValueError):
    """A locally authored safe failure; native diagnostics stay in caller logs."""


def resolve_sensitivity(profile=None, overrides=None):
    """Resolve one named sensitivity profile plus explicit parameter overrides.

    Returns the resolved parameters together with their provenance, so the
    acoustic document records *why* these boundaries were produced and whether
    they are the canonical measurement policy.
    """
    name = profile or CANONICAL_SENSITIVITY
    if name not in SENSITIVITY_PROFILES:
        raise AcousticError('Unknown acoustic sensitivity profile: ' + str(name))
    parameters = dict(SENSITIVITY_PROFILES[name])
    applied = {}
    for key, value in (overrides or {}).items():
        if key not in parameters:
            raise AcousticError('Unknown acoustic sensitivity override: ' + str(key))
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise AcousticError('Acoustic sensitivity overrides must be finite and non-negative: ' + key)
        parameters[key] = value
        applied[key] = value
    canonical = name == CANONICAL_SENSITIVITY and not applied
    return {
        'profile': name,
        'parameters': parameters,
        'overrides': applied,
        'is_canonical_measurement_policy': canonical,
        'note': ('Canonical acoustic measurement policy.' if canonical else
                 'Non-canonical acoustic sensitivity: produced for evaluating quiet '
                 'device coverage. Results from this profile must not be reported as '
                 'the canonical measurement.'),
    }


@dataclass
class AcousticSegment:
    """A speech-segment candidate with acoustic evidence."""

    start_ms: float
    end_ms: float
    confidence: float
    method: str
    uncertainty_ms: float
    peak_rms: float
    mean_rms: float
    threshold_rms: float
    frame_count: int

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'confidence': round(self.confidence, 4),
            'source': 'acoustic',
            'method': self.method,
            'uncertainty_ms': round(self.uncertainty_ms, 3),
            'frame_stats': {
                'peak_rms': round(self.peak_rms, 6),
                'mean_rms': round(self.mean_rms, 6),
                'threshold_rms': round(self.threshold_rms, 6),
                'frame_count': self.frame_count,
            },
        }


@dataclass
class AcousticResult:
    segments: list  # list[AcousticSegment]
    status: str
    reason: str | None
    source: dict
    processor: dict

    def to_dict(self):
        return {
            'schema_version': '1.0.0',
            'document_id': 'ACOUSTIC-' + uuid.uuid4().hex,
            'source': self.source,
            'processor': self.processor,
            'status': self.status,
            'reason': self.reason,
            'segments': [seg.to_dict(f'SEG-{i:04d}') for i, seg in enumerate(self.segments)],
        }


class AcousticSegmenter(Protocol):
    def segment(self, path: Path) -> AcousticResult: ...


def _read_canonical(path):
    """Stream-decode PCM16 mono WAV; return samples and metadata."""
    path = Path(path)
    try:
        with wave.open(str(path), 'rb') as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            rate = audio.getframerate()
            comptype = audio.getcomptype()
            if (channels, sample_width, rate, comptype) != (CANONICAL_CHANNELS, CANONICAL_SAMPLE_WIDTH,
                                                            CANONICAL_SAMPLE_RATE, 'NONE'):
                raise AcousticError(
                    f'Acoustic segmentation requires WAV PCM16LE {CANONICAL_SAMPLE_RATE} Hz mono; '
                    f'got {channels}ch {sample_width * 8}-bit {rate} Hz {comptype}')
            frames = audio.getnframes()
            raw = audio.readframes(frames)
            if len(raw) != frames * CANONICAL_SAMPLE_WIDTH:
                raise AcousticError('Truncated canonical WAV sample data')
            values = array('h', raw)
            if sys.byteorder != 'little':
                values.byteswap()
    except wave.Error as error:
        raise AcousticError(f'Cannot parse WAV: {error}') from None
    except OSError as error:
        raise AcousticError(f'Audio file is unavailable: {error}') from None
    return values, frames, rate, channels, sample_width


def _frame_energies(samples, frame_size, hop_size):
    """RMS energy per analysis frame (linear, not dB)."""
    n = len(samples)
    if frame_size <= 0 or hop_size <= 0:
        raise AcousticError('Frame and hop sizes must be positive')
    energies = []
    positions = []
    for offset in range(0, max(1, n - frame_size + 1), hop_size):
        chunk = samples[offset:offset + frame_size]
        if len(chunk) < frame_size:
            break
        total = sum(value * value for value in chunk)
        rms = math.sqrt(total / frame_size) / 32768.0
        energies.append(rms)
        positions.append(offset)
    return energies, positions


def _percentile(sorted_values, pct):
    """R7 linear interpolation percentile on a pre-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * pct / 100.0
    lower = math.floor(pos)
    upper = math.ceil(pos)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (pos - lower)


def _smooth_mask(mask, merge_gap_frames):
    """Merge speech gaps shorter than ``merge_gap_frames`` into continuous speech."""
    if not mask:
        return mask
    result = list(mask)
    gap_start = None
    for i in range(1, len(result)):
        if not result[i] and result[i - 1]:
            gap_start = i
        elif result[i] and gap_start is not None:
            gap_len = i - gap_start
            if gap_len <= merge_gap_frames:
                for j in range(gap_start, i):
                    result[j] = True
            gap_start = None
    return result


def _segments_from_mask(mask, positions, frame_size, sample_rate,
                        min_speech_samples, min_silence_samples, merge_gap_frames,
                        pre_roll_samples, post_roll_samples):
    """Extract contiguous speech intervals from a boolean frame mask."""
    mask = _smooth_mask(mask, merge_gap_frames)
    segments_raw = []
    in_speech = False
    start = 0
    for i, is_speech in enumerate(mask):
        if is_speech and not in_speech:
            start = positions[i]
            in_speech = True
        elif not is_speech and in_speech:
            end = positions[i - 1] + frame_size
            segments_raw.append((start, end))
            in_speech = False
    if in_speech:
        segments_raw.append((start, positions[-1] + frame_size if positions else frame_size))

    # Apply pre/post roll and minimum-duration filter.
    result = []
    for seg_start, seg_end in segments_raw:
        seg_start = max(0, seg_start - pre_roll_samples)
        seg_end = seg_end + post_roll_samples
        if seg_end - seg_start >= min_speech_samples:
            result.append((seg_start, seg_end))
    return result


class EnergyVadSegmenter:
    """Frame-based energy VAD with adaptive noise-floor threshold.

    Cloud or model-based VAD can implement the same ``AcousticSegmenter``
    Protocol.  This local implementation uses only stdlib signal processing.
    """

    def __init__(self, frame_ms=DEFAULT_FRAME_MS, hop_ms=DEFAULT_HOP_MS,
                 threshold_factor=DEFAULT_THRESHOLD_FACTOR,
                 min_speech_ms=DEFAULT_MIN_SPEECH_MS,
                 min_silence_ms=DEFAULT_MIN_SILENCE_MS,
                 merge_gap_ms=DEFAULT_MERGE_GAP_MS,
                 pre_roll_ms=DEFAULT_PRE_ROLL_MS,
                 post_roll_ms=DEFAULT_POST_ROLL_MS,
                 sensitivity=None):
        if frame_ms <= 0 or hop_ms <= 0 or hop_ms > frame_ms:
            raise AcousticError('frame_ms must be positive and >= hop_ms')
        if not 0 < threshold_factor < 1:
            raise AcousticError('threshold_factor must be in (0, 1)')
        if min_speech_ms <= 0 or min_silence_ms <= 0:
            raise AcousticError('min_speech_ms and min_silence_ms must be positive')
        if merge_gap_ms < 0 or pre_roll_ms < 0 or post_roll_ms < 0:
            raise AcousticError('merge_gap_ms, pre_roll_ms, post_roll_ms must be non-negative')
        self.frame_ms = frame_ms
        self.hop_ms = hop_ms
        self.threshold_factor = threshold_factor
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self.merge_gap_ms = merge_gap_ms
        self.pre_roll_ms = pre_roll_ms
        self.post_roll_ms = post_roll_ms
        # Provenance of the sensitivity actually used. Defaults to the canonical
        # measurement policy; a caller-supplied record marks a comparison run.
        self.sensitivity = sensitivity or resolve_sensitivity(CANONICAL_SENSITIVITY)

    @classmethod
    def from_profile(cls, profile=None, overrides=None, frame_ms=DEFAULT_FRAME_MS,
                     hop_ms=DEFAULT_HOP_MS):
        """Build a segmenter from a named sensitivity profile (plus overrides)."""
        resolved = resolve_sensitivity(profile, overrides)
        return cls(frame_ms=frame_ms, hop_ms=hop_ms, sensitivity=resolved,
                   **resolved['parameters'])

    @property
    def method(self):
        return 'energy_vad'

    @property
    def processor_version(self):
        return '1.0.0'

    def _parameters(self, sample_rate):
        frame_size = int(round(self.frame_ms / 1000.0 * sample_rate))
        hop_size = int(round(self.hop_ms / 1000.0 * sample_rate))
        min_speech_samples = int(round(self.min_speech_ms / 1000.0 * sample_rate))
        min_silence_samples = int(round(self.min_silence_ms / 1000.0 * sample_rate))
        merge_gap_frames = max(0, int(round(self.merge_gap_ms / self.hop_ms)))
        pre_roll_samples = int(round(self.pre_roll_ms / 1000.0 * sample_rate))
        post_roll_samples = int(round(self.post_roll_ms / 1000.0 * sample_rate))
        return (frame_size, hop_size, min_speech_samples, min_silence_samples,
                merge_gap_frames, pre_roll_samples, post_roll_samples)

    def segment(self, path):
        path = Path(path).resolve()
        samples, frame_count, rate, channels, sample_width = _read_canonical(path)
        sha256 = digest(path)
        duration_ms = frame_count / rate * 1000.0

        (frame_size, hop_size, min_speech_samples, min_silence_samples,
         merge_gap_frames, pre_roll_samples, post_roll_samples) = self._parameters(rate)

        energies, positions = _frame_energies(samples, frame_size, hop_size)

        source = {
            'path': str(path),
            'sha256': sha256,
            'duration_ms': round(duration_ms, 3),
            'sample_rate_hz': rate,
            'channels': channels,
            'encoding': 'PCM_S16LE',
        }
        params = {
            'frame_ms': self.frame_ms,
            'hop_ms': self.hop_ms,
            'energy_metric': 'rms',
            'threshold_factor': self.threshold_factor,
            'threshold_mode': 'noise_floor_plus_active_range_fraction',
            'min_speech_ms': self.min_speech_ms,
            'min_silence_ms': self.min_silence_ms,
            'merge_gap_ms': self.merge_gap_ms,
            'pre_roll_ms': self.pre_roll_ms,
            'post_roll_ms': self.post_roll_ms,
        }
        processor = {
            'method': self.method,
            'processor_version': self.processor_version,
            'parameters': params,
            'sensitivity': {
                'profile': self.sensitivity['profile'],
                'overrides': dict(self.sensitivity['overrides']),
                'is_canonical_measurement_policy': self.sensitivity['is_canonical_measurement_policy'],
                'note': self.sensitivity['note'],
            },
        }

        if not energies:
            return AcousticResult(
                segments=[], status='insufficient_evidence',
                reason='Audio is shorter than one analysis frame',
                source=source, processor=processor)

        sorted_energies = sorted(energies)
        noise_floor = _percentile(sorted_energies, 10)
        peak_energy = max(energies)
        if peak_energy <= SILENCE_FLOOR:
            return AcousticResult(
                segments=[], status='insufficient_evidence',
                reason='Audio is entirely silent; no speech segment can be claimed',
                source=source, processor=processor)

        threshold = noise_floor + (peak_energy - noise_floor) * self.threshold_factor
        threshold = max(threshold, noise_floor * 3.0)

        mask = [energy >= threshold for energy in energies]

        intervals = _segments_from_mask(
            mask, positions, frame_size, rate,
            min_speech_samples, min_silence_samples, merge_gap_frames,
            pre_roll_samples, post_roll_samples)

        segments = []
        for seg_start_sample, seg_end_sample in intervals:
            seg_start_ms = seg_start_sample / rate * 1000.0
            seg_end_ms = seg_end_sample / rate * 1000.0
            # Confidence: how far above threshold the peak frame energy is,
            # normalised against the active range.  Bounded to [0, 1].
            seg_energies = [energies[i] for i in range(len(energies))
                            if positions[i] + frame_size > seg_start_sample
                            and positions[i] < seg_end_sample]
            if not seg_energies:
                continue
            seg_peak = max(seg_energies)
            seg_mean = sum(seg_energies) / len(seg_energies)
            active_range = peak_energy - noise_floor
            if active_range > SILENCE_FLOOR:
                confidence = min(1.0, (seg_peak - threshold) / active_range)
            else:
                confidence = 0.5
            # Uncertainty is the hop resolution: the true onset could be
            # anywhere within one hop of the detected boundary.
            uncertainty_ms = self.hop_ms
            segments.append(AcousticSegment(
                start_ms=seg_start_ms, end_ms=seg_end_ms,
                confidence=confidence, method=self.method,
                uncertainty_ms=uncertainty_ms,
                peak_rms=seg_peak, mean_rms=seg_mean,
                threshold_rms=threshold, frame_count=len(seg_energies)))

        status = 'complete' if segments else 'insufficient_evidence'
        reason = None if segments else 'No speech segments met the minimum-duration threshold'

        return AcousticResult(
            segments=segments, status=status, reason=reason,
            source=source, processor=processor)


def segment_audio(path, output=None, segmenter=None):
    """Segment a canonical WAV and optionally write the result JSON.

    Returns ``(result_dict, result_path_or_None)``.
    """
    segmenter = segmenter or EnergyVadSegmenter()
    result = segmenter.segment(path)
    document = result.to_dict()
    out_path = None
    if output is not None:
        out_path = Path(output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, document)
    return document, out_path
