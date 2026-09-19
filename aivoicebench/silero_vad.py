"""Silero VAD acoustic-boundary provider (Issue #23).

This module is the first *model-based* implementation of the
:class:`aivoicebench.acoustic.AcousticSegmenter` Protocol. It produces
speech-segment **candidates** with deterministic ``audio_relative_ms``
coordinates, processor/model provenance, versioned policy inputs and explicit
uncertainty. It never produces speaker roles, never treats ASR timestamps as
acoustic truth and never replaces the legacy energy VAD silently.

Design boundaries (see ``docs/17-acoustic-segmentation.md``):

* **Runtime.** The model is executed through ``onnxruntime`` directly with the
  official Silero ONNX export. The adapter does not import ``torch``: the
  bundled torch/torchaudio pair is not needed for inference and is a large,
  non-deterministic-quality dependency surface. Only ``onnxruntime`` (model
  execution) and ``numpy`` are imported at run time.
* **Weights.** The weights are taken from the ``silero_vad`` PyPI wheel
  (``silero_vad/data/silero_vad.onnx``, package ``6.2.2``, file sha256
  :data:`SILERO_VAD_MODEL_SHA256_6_2_2`). The adapter verifies the file size and
  digest of whatever path it loads and records the digest it actually used, so a
  swapped or truncated model cannot be reported as the verified one. No model
  binary is committed to this repository.
* **Policy.** Thresholds and post-processing are AIVoiceBench measurement inputs,
  not upstream defaults and not product truth. They live in the versioned
  :class:`SileroVadPolicy`; the effective values are recorded in the document.
  :data:`SILERO_POLICY_NOTES` states that the shipped defaults are unvalidated
  starting points and that this module makes **no accuracy claim**.
* **No silent fallback.** If the adapter cannot load ``onnxruntime``, cannot find
  the model file, fails the digest/size check or cannot bind the model's I/O, it
  raises :class:`SileroUnavailableError`. Callers must either fail, or *explicitly*
  select the energy VAD as a fallback and record that choice; nothing here
  substitutes a different algorithm for ``silero_vad``.
"""

from dataclasses import dataclass
import hashlib
import importlib.util
import math
import os
from pathlib import Path

from .acoustic import (
    AcousticError, AcousticResult, AcousticSegment, CANONICAL_SAMPLE_RATE,
    _read_canonical,
)
from .runner import digest

#: ``method`` recorded for every document this provider produces.
SILERO_METHOD = 'silero_vad'

#: Version of *this adapter + policy family*. Bump when the boundary decision
#: procedure, the framing or the reported provenance changes meaning.
SILERO_PROCESSOR_VERSION = '1.0.0'

#: Name recorded in ``processor.model.name``.
SILERO_MODEL_NAME = 'silero_vad'

#: The Silero model version these weights belong to.
SILERO_MODEL_VERSION = '6.2.0'

#: Package the weights are taken from, and the exact version that was audited.
SILERO_MODEL_SOURCE = 'silero-vad PyPI wheel 6.2.2: silero_vad/data/silero_vad.onnx'
SILERO_MODEL_PACKAGE = 'silero-vad'
SILERO_MODEL_PACKAGE_VERSION = '6.2.2'

#: Expected size and sha256 of ``silero_vad/data/silero_vad.onnx`` shipped by
#: ``silero-vad==6.2.2``. Recorded here so a load can be checked against the
#: audited weight file instead of trusting whichever version happens to be
#: installed. This is an integrity check with an explicit failure, not a
#: fallback: a mismatch raises.
SILERO_VAD_MODEL_BYTES_6_2_2 = 2327524
SILERO_VAD_MODEL_SHA256_6_2_2 = '1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3'

#: The model consumes fixed 512-sample windows at 16 kHz plus a 64-sample
#: left context it carries across calls. Both are properties of the exported
#: graph, not policy choices.
SILERO_WINDOW_SAMPLES = 512
SILERO_CONTEXT_SAMPLES = 64

#: Environment override for a deployment-provided model file. It only changes
#: *where* the weights come from; the integrity check and provenance recording
#: still apply, and the document says which path was used.
MODEL_PATH_ENV = 'AIVOICEBENCH_SILERO_MODEL'

#: Environment override selecting the policy version. Only policy versions
#: implemented here are accepted; an unknown value raises.
POLICY_VERSION_ENV = 'AIVOICEBENCH_SILERO_POLICY'

#: Named analysis policies. A named policy is a *versioned AIVoiceBench
#: measurement input*, not a Silero default: the upstream defaults were recorded
#: as the initial baseline, and the record states that they are unvalidated.
SILERO_POLICY_NOTES = (
    'AIVoiceBench measurement input, versioned as silero_boundary_policy/1.0.0. '
    'The threshold, hysteresis gap and minimum speech/silence durations are the '
    'initial baseline taken from the upstream Silero reference post-processing so '
    'the provider is deterministic and auditable; they were NOT calibrated on '
    'AIVoiceBench target recordings. No accuracy claim is made for this policy '
    'and it is not a product threshold. Policy changes require a new '
    'policy_version, not an edit in place.'
)


class SileroUnavailableError(AcousticError):
    """The requested model provider cannot run here.

    Raised with the concrete reason (missing runtime, missing weights, failed
    integrity check, incompatible graph). Callers may fall back to the energy VAD
    **only** by an explicit decision that is recorded; this exception is never
    swallowed inside this module.
    """


class SileroPolicyError(AcousticError):
    """The requested named policy or policy parameter is invalid."""


@dataclass(frozen=True)
class SileroVadPolicy:
    """Versioned acoustic-boundary policy for the Silero provider.

    Every member is a decision the *measurement* makes, so every member is
    recorded in the document. Defaults are the audited ``1.0.0`` baseline; see
    :data:`SILERO_POLICY_NOTES` for what that does and does not claim.
    """

    version: str = '1.0.0'
    #: Speech onset threshold on the model's per-window speech probability.
    threshold: float = 0.5
    #: Exit threshold (hysteresis). Onset needs ``>= threshold``; release needs
    #: ``< negative_threshold``. Must be below ``threshold``.
    negative_threshold: float = 0.35
    #: A candidate segment shorter than this is discarded (unknown, not silence).
    min_speech_ms: float = 250.0
    #: Silence shorter than this does not end a segment.
    min_silence_ms: float = 100.0
    #: Speech regions separated by a shorter gap are merged.
    merge_gap_ms: float = 0.0
    #: Symmetric roll added to every accepted boundary. The model's own
    #: post-processing pads by half this on each side; AIVoiceBench records it as
    #: one explicit policy member instead of hiding it.
    pre_roll_ms: float = 30.0
    post_roll_ms: float = 30.0

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise SileroPolicyError('Silero policy version must be a non-empty string')
        for name in ('threshold', 'negative_threshold'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
                raise SileroPolicyError(f'Silero policy {name} must be a finite value in (0, 1)')
        if self.negative_threshold > self.threshold:
            raise SileroPolicyError('Silero policy negative_threshold must not exceed threshold')
        for name in ('min_speech_ms', 'min_silence_ms'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise SileroPolicyError(f'Silero policy {name} must be a finite positive value')
        for name in ('merge_gap_ms', 'pre_roll_ms', 'post_roll_ms'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise SileroPolicyError(f'Silero policy {name} must be a finite non-negative value')

    @property
    def name(self):
        return 'silero_boundary_policy/' + self.version

    def to_parameters(self):
        """Effective parameters as recorded in ``processor.parameters``."""
        return {
            'frame_samples': SILERO_WINDOW_SAMPLES,
            'hop_samples': SILERO_WINDOW_SAMPLES,
            'threshold': float(self.threshold),
            'negative_threshold': float(self.negative_threshold),
            'min_speech_ms': float(self.min_speech_ms),
            'min_silence_ms': float(self.min_silence_ms),
            'merge_gap_ms': float(self.merge_gap_ms),
            'pre_roll_ms': float(self.pre_roll_ms),
            'post_roll_ms': float(self.post_roll_ms),
        }


#: Named, versioned policies. ``silero_boundary_policy/1.0.0`` is the only
#: calibrated-on-nothing baseline that exists; anything else must be added as a
#: new entry with its own notes rather than by mutating this one.
SILERO_POLICIES = {
    '1.0.0': SileroVadPolicy,
}


def resolve_policy(policy=None):
    """Resolve a named policy version, the ``AIVOICEBENCH_SILERO_POLICY`` default
    or an explicit :class:`SileroVadPolicy`.

    Returns ``(policy, provenance)`` where ``provenance`` records how the policy
    was selected, so a document always says which policy produced its boundaries.
    """
    if policy is None:
        requested = (os.environ.get(POLICY_VERSION_ENV) or '').strip() or '1.0.0'
        factory = SILERO_POLICIES.get(requested)
        if factory is None:
            raise SileroPolicyError(
                'Unknown Silero policy version: ' + requested
                + '; implemented: ' + ', '.join(sorted(SILERO_POLICIES)))
        resolved = factory()
        selected = 'named_version'
    elif isinstance(policy, SileroVadPolicy):
        resolved = policy
        selected = 'explicit'
    elif isinstance(policy, str):
        factory = SILERO_POLICIES.get(policy.strip())
        if factory is None:
            raise SileroPolicyError(
                'Unknown Silero policy version: ' + str(policy)
                + '; implemented: ' + ', '.join(sorted(SILERO_POLICIES)))
        resolved = factory()
        selected = 'named_version'
    else:
        raise SileroPolicyError('Silero policy must be a version string or SileroVadPolicy')
    provenance = {
        'name': resolved.name,
        'selected_by': selected,
        'is_canonical_measurement_policy': False,
        'note': SILERO_POLICY_NOTES,
    }
    return resolved, provenance


def model_path():
    """Return the absolute path of the ONNX weights this provider will load.

    Resolution order: the ``AIVOICEBENCH_SILERO_MODEL`` override, then the file
    shipped inside the installed ``silero_vad`` package. The package location is
    resolved without importing ``silero_vad`` (importing it pulls in torch), so
    the provider has no torch dependency.
    """
    override = (os.environ.get(MODEL_PATH_ENV) or '').strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise SileroUnavailableError(
                f'{MODEL_PATH_ENV} points at a missing Silero model file: {candidate}')
        return candidate.resolve()
    spec = importlib.util.find_spec('silero_vad')
    if spec is None:
        raise SileroUnavailableError(
            'Silero VAD weights are unavailable: the silero_vad package is not installed. '
            f'Install it (for example "pip install -r requirements-vad.txt") or point '
            f'{MODEL_PATH_ENV} at an ONNX model file.')
    roots = []
    if spec.submodule_search_locations:
        roots.extend(Path(location) for location in spec.submodule_search_locations)
    elif spec.origin:
        roots.append(Path(spec.origin).parent)
    for root in roots:
        candidate = root / 'data' / 'silero_vad.onnx'
        if candidate.is_file():
            return candidate.resolve()
    raise SileroUnavailableError(
        'Silero VAD weights are unavailable: silero_vad is importable but '
        'data/silero_vad.onnx was not found under ' + ', '.join(str(root) for root in roots))


def _sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def inspect_model(path=None):
    """Audit the weight file without loading a runtime.

    Returns the provenance block that a document records: name, version, source,
    digest, size and the model's fixed framing. Raises
    :class:`SileroUnavailableError` when the file is missing or its size does not
    match the audited wheel, so a wrong or truncated weight file cannot be
    reported as the verified one.
    """
    resolved = Path(path).resolve() if path is not None else model_path()
    if not resolved.is_file():
        raise SileroUnavailableError('Silero VAD model file is unavailable: ' + str(resolved))
    size = resolved.stat().st_size
    if size != SILERO_VAD_MODEL_BYTES_6_2_2:
        raise SileroUnavailableError(
            f'Silero VAD model size mismatch: {resolved} is {size} bytes, the audited '
            f'silero-vad {SILERO_MODEL_PACKAGE_VERSION} weight file is '
            f'{SILERO_VAD_MODEL_BYTES_6_2_2} bytes. Refusing to report unverified weights as '
            'the audited model.')
    actual = _sha256(resolved)
    if actual != SILERO_VAD_MODEL_SHA256_6_2_2:
        raise SileroUnavailableError(
            f'Silero VAD model digest mismatch: {resolved} is {actual}, the audited '
            f'silero-vad {SILERO_MODEL_PACKAGE_VERSION} weight file is '
            f'{SILERO_VAD_MODEL_SHA256_6_2_2}. Refusing to report unverified weights as the '
            'audited model.')
    return {
        'name': SILERO_MODEL_NAME,
        'version': SILERO_MODEL_VERSION,
        'sha256': actual,
        'source': SILERO_MODEL_SOURCE,
        'package': SILERO_MODEL_PACKAGE,
        'package_version': SILERO_MODEL_PACKAGE_VERSION,
        'path': str(resolved),
        'bytes': size,
        'sample_rate_hz': CANONICAL_SAMPLE_RATE,
        'window_samples': SILERO_WINDOW_SAMPLES,
        'context_samples': SILERO_CONTEXT_SAMPLES,
    }


class _OnnxRuntime:
    """Thin, dependency-checked wrapper around one ONNX Runtime session.

    The session is single-threaded so the same Artifact replays to the same
    probabilities. Nothing here writes to disk or the network.
    """

    def __init__(self, model_path):
        try:
            import numpy
        except ImportError as error:  # pragma: no cover - numpy is a project dep
            raise SileroUnavailableError(
                'Silero VAD requires numpy, which is not installed: ' + str(error)) from None
        try:
            import onnxruntime
        except ImportError as error:
            raise SileroUnavailableError(
                'Silero VAD requires the onnxruntime package, which is not installed: '
                + str(error) + f'. Install it with "pip install -r requirements-vad.txt".') from None
        self.numpy = numpy
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.log_severity_level = 3
        try:
            self.session = onnxruntime.InferenceSession(
                str(model_path), sess_options=options, providers=['CPUExecutionProvider'])
        except Exception as error:
            raise SileroUnavailableError(
                f'Cannot load the Silero VAD ONNX model at {model_path}: '
                f'{type(error).__name__}: {error}') from None
        inputs = {item.name: item for item in self.session.get_inputs()}
        outputs = [item.name for item in self.session.get_outputs()]
        missing = {'input', 'state', 'sr'} - set(inputs)
        if missing or not outputs:
            raise SileroUnavailableError(
                'Silero VAD ONNX model has an unexpected interface; missing inputs: '
                + ', '.join(sorted(missing)) + f'; outputs: {outputs}')
        self.runtime = 'onnxruntime'
        self.runtime_version = getattr(onnxruntime, '__version__', 'unknown')
        self.execution_provider = 'CPUExecutionProvider'

    def probabilities(self, samples):
        """Per-window speech probabilities for float32 samples in ``[-1, 1)``."""
        numpy = self.numpy
        state = numpy.zeros((2, 1, 128), dtype=numpy.float32)
        context = numpy.zeros((1, SILERO_CONTEXT_SAMPLES), dtype=numpy.float32)
        rate = numpy.array(CANONICAL_SAMPLE_RATE, dtype=numpy.int64)
        probabilities = []
        last_window_padded = 0
        total = len(samples)
        for offset in range(0, total, SILERO_WINDOW_SAMPLES):
            chunk = numpy.asarray(samples[offset:offset + SILERO_WINDOW_SAMPLES], dtype=numpy.float32)
            if chunk.size == 0:
                break
            padded = SILERO_WINDOW_SAMPLES - int(chunk.size)
            if padded:
                # The exported graph needs a full window. Zero padding is the
                # upstream behaviour; the amount is reported as tail_padded so an
                # audit knows the last window was not fully measured.
                chunk = numpy.pad(chunk, (0, padded))
            window = numpy.concatenate([context, chunk.reshape(1, -1)], axis=1)
            output, state = self.session.run(
                None, {'input': window, 'state': state, 'sr': rate})
            context = window[..., -SILERO_CONTEXT_SAMPLES:]
            probabilities.append(float(output.reshape(-1)[0]))
            last_window_padded = padded
        # Only the final window may be partial; every earlier window is complete.
        return probabilities, last_window_padded


def _histogram(probabilities):
    """Deterministic 10-bin histogram over ``[0, 1]``; ``1.0`` lands in the last bin."""
    bins = [0] * 10
    for value in probabilities:
        index = int(value * 10)
        if index < 0:
            index = 0
        elif index > 9:
            index = 9
        bins[index] += 1
    return bins


def _decide(probabilities, policy, sample_rate):
    """Turn per-window probabilities into raw ``[start, end)`` sample intervals.

    Hysteresis state machine: onset at ``probability >= threshold``, release once
    ``probability < negative_threshold`` has persisted for ``min_silence_ms``.
    Candidate segments shorter than ``min_speech_ms`` are dropped (they stay
    unclaimed, not silently reclassified). A segment still open at the end of the
    audio is closed at the last measured sample; there is no fabricated maximum
    speech length.
    """
    window = SILERO_WINDOW_SAMPLES
    hop = SILERO_WINDOW_SAMPLES
    min_silence_samples = int(round(policy.min_silence_ms / 1000.0 * sample_rate))
    min_speech_samples = int(round(policy.min_speech_ms / 1000.0 * sample_rate))
    intervals = []
    triggered = False
    start = 0
    silence_run = 0
    silence_begin = 0
    for index, probability in enumerate(probabilities):
        position = index * hop
        if not triggered:
            if probability >= policy.threshold:
                triggered = True
                start = position
                silence_run = 0
            continue
        if probability < policy.negative_threshold:
            if silence_run == 0:
                silence_begin = position
            # Window index i covers [i*hop, i*hop + window).
            silence_run = position + window - silence_begin
            if silence_run >= min_silence_samples:
                end = silence_begin
                if end - start >= min_speech_samples:
                    intervals.append((start, end))
                triggered = False
                silence_run = 0
        else:
            silence_run = 0
    if triggered:
        end = len(probabilities) * hop
        if end - start >= min_speech_samples:
            intervals.append((start, end))
    return intervals


def _apply_roll(intervals, policy, sample_rate, total_samples):
    """Apply the policy's pre/post roll and clamp to the artifact duration."""
    pre = int(round(policy.pre_roll_ms / 1000.0 * sample_rate))
    post = int(round(policy.post_roll_ms / 1000.0 * sample_rate))
    return [(max(0, start - pre), min(total_samples, end + post))
            for start, end in intervals]


def _merge(intervals, policy, sample_rate):
    """Merge intervals whose gap is at most ``merge_gap_ms``."""
    if not intervals:
        return []
    max_gap = int(round(policy.merge_gap_ms / 1000.0 * sample_rate))
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        previous_start, previous_end = merged[-1]
        if start - previous_end <= max_gap:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


class SileroVadSegmenter:
    """Model-based acoustic-boundary provider implementing ``AcousticSegmenter``.

    The instance owns one loaded ONNX session. ``segment()`` is the Protocol
    method; it reads only the canonical WAV it is given, so ASR or diarization
    output cannot enter the acoustic result.
    """

    def __init__(self, model=None, policy=None):
        self.policy, self.policy_provenance = resolve_policy(policy)
        self.model_path = Path(model).resolve() if model is not None else model_path()
        self.model = inspect_model(self.model_path)
        self._runtime = None

    @property
    def method(self):
        return SILERO_METHOD

    @property
    def processor_version(self):
        return SILERO_PROCESSOR_VERSION

    def runtime(self):
        """Lazily load the ONNX session (kept lazy so inspecting a policy or a
        weight file never needs a runtime)."""
        if self._runtime is None:
            self._runtime = _OnnxRuntime(self.model_path)
        return self._runtime

    def _processor(self, runtime, probabilities, tail_padded, frames):
        parameters = self.policy.to_parameters()
        if probabilities:
            parameters['frames_above_threshold'] = sum(
                1 for value in probabilities if value >= self.policy.threshold)
            parameters['frames_below_negative_threshold'] = sum(
                1 for value in probabilities if value < self.policy.negative_threshold)
            parameters['speech_probability_histogram'] = _histogram(probabilities)
        model_block = {
            'name': self.model['name'],
            'version': self.model['version'],
            'sha256': self.model['sha256'],
            'source': self.model['source'],
            'runtime': runtime.runtime,
            'runtime_version': runtime.runtime_version,
            'execution_provider': runtime.execution_provider,
            'sample_rate_hz': CANONICAL_SAMPLE_RATE,
            'window_samples': SILERO_WINDOW_SAMPLES,
            'context_samples': SILERO_CONTEXT_SAMPLES,
            'tail_padded': bool(tail_padded),
            'frames': frames,
        }
        if probabilities:
            model_block['speech_probability_mean'] = _round_probability(
                sum(probabilities) / len(probabilities))
            model_block['speech_probability_max'] = _round_probability(max(probabilities))
            model_block['speech_probability_min'] = _round_probability(min(probabilities))
        # The measurement-policy provenance reuses the contract's optional
        # `processor.sensitivity` block: `profile` carries the policy id, so a
        # Silero document is explicit about which policy produced its boundaries
        # and never claims to be the canonical measurement policy.
        processor = {
            'method': SILERO_METHOD,
            'processor_version': SILERO_PROCESSOR_VERSION,
            'parameters': parameters,
            'sensitivity': {
                'profile': self.policy.name,
                'overrides': {},
                'is_canonical_measurement_policy':
                    self.policy_provenance['is_canonical_measurement_policy'],
                'note': SILERO_POLICY_NOTES,
            },
        }
        return processor, {'model': model_block}

    def segment(self, path):
        path = Path(path).resolve()
        samples, frame_count, rate, channels, _ = _read_canonical(path, scale=True)
        sha256 = digest(path)
        duration_ms = frame_count / rate * 1000.0
        source = {
            'path': str(path),
            'sha256': sha256,
            'duration_ms': round(duration_ms, 3),
            'sample_rate_hz': rate,
            'channels': channels,
            'encoding': 'PCM_S16LE',
        }
        runtime = self.runtime()
        if 0 < frame_count < SILERO_WINDOW_SAMPLES:
            # Do not spend a model call on audio with no complete window: the
            # single window would be entirely zero padding, which reports on the
            # padding rather than on the recording.
            processor, processor_extra = self._processor(runtime, [], 0, 0)
            return AcousticResult(
                segments=[], status='insufficient_evidence',
                reason=('Audio is shorter than one Silero analysis window '
                        f'({SILERO_WINDOW_SAMPLES} samples at 16 kHz)'),
                source=source, processor=processor, processor_extra=processor_extra)
        probabilities, tail_padded = (
            runtime.probabilities(samples) if samples else ([], 0))

        processor, processor_extra = self._processor(
            runtime, probabilities, tail_padded, len(probabilities))

        if not probabilities:
            return AcousticResult(
                segments=[], status='insufficient_evidence',
                reason='No Silero analysis window could be evaluated for this audio',
                source=source, processor=processor, processor_extra=processor_extra)

        intervals = _decide(probabilities, self.policy, rate)
        intervals = _apply_roll(intervals, self.policy, rate, frame_count)
        intervals = _merge(intervals, self.policy, rate)

        segments = []
        for start, end in intervals:
            window_indexes = [
                index for index in range(len(probabilities))
                if index * SILERO_WINDOW_SAMPLES + SILERO_WINDOW_SAMPLES > start
                and index * SILERO_WINDOW_SAMPLES < end
            ]
            if not window_indexes:
                continue
            values = [probabilities[index] for index in window_indexes]
            above = [value for value in values if value >= self.policy.threshold]
            # Confidence is the share of windows that cleared the onset
            # threshold, floored by how far the peak cleared it. It expresses
            # producer confidence, not a calibrated probability.
            share = len(above) / len(values)
            margin = max(0.0, (max(values) - self.policy.threshold)
                         / max(1e-9, 1.0 - self.policy.threshold))
            confidence = min(1.0, max(share, margin))
            stats = {
                'peak_speech_probability': _round_probability(max(values)),
                'mean_speech_probability': _round_probability(sum(values) / len(values)),
                'min_speech_probability': _round_probability(min(values)),
                'threshold': float(self.policy.threshold),
                'frames_above_threshold': len(above),
                'frame_count': len(values),
            }
            segments.append(AcousticSegment(
                start_ms=start / rate * 1000.0,
                end_ms=end / rate * 1000.0,
                confidence=confidence,
                method=SILERO_METHOD,
                # One analysis window: the true onset can be anywhere inside the
                # window the decision was made from.
                uncertainty_ms=SILERO_WINDOW_SAMPLES / rate * 1000.0,
                # No RMS is measured here, so the energy fields stay unset rather
                # than being reported as a measured zero. `frame_stats` carries the
                # model evidence this boundary actually came from.
                frame_stats=stats))

        status = 'complete' if segments else 'insufficient_evidence'
        reason = None if segments else (
            'No speech region met the policy minimum speech duration ('
            f'{self.policy.min_speech_ms:g} ms) at threshold {self.policy.threshold:g}')
        return AcousticResult(
            segments=segments, status=status, reason=reason,
            source=source, processor=processor, processor_extra=processor_extra)


def _round_probability(value):
    return round(float(value), 6)


#: Fields of a document that are *identity or environment*, not measurement.
#: ``document_id`` is a fresh UUID per emission and ``source.path`` is where the
#: caller happened to keep the file; neither describes the acoustic evidence.
NON_MEASUREMENT_FIELDS = ('document_id', 'source.path')


def normalized_evidence(document):
    """Strip identity/environment fields, keeping every measurement field.

    Used to compare two runs of the same processor over the same Artifact and
    policy (acceptance criterion: deterministic replay). The returned structure
    still contains the source digest/duration/format, so the comparison is bound
    to the exact Artifact bytes, not to a filename.

    The result is a detached deep copy, so a caller may keep comparing it after
    its input document has moved on.
    """
    import json
    stripped = dict(document)
    stripped.pop('document_id', None)
    source = dict(stripped.get('source') or {})
    source.pop('path', None)
    stripped['source'] = source
    return json.loads(json.dumps(stripped, allow_nan=False))
