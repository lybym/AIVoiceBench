"""Optional local audio I/O. Importing this module never opens a microphone."""

import math
from pathlib import Path
import threading
import time
import uuid
import wave

from .runner import digest, inspect_audio, write_json
from .validation import load_document, schema_errors


def _dependencies():
    try:
        import numpy as np
        import sounddevice as sd
    except (ImportError, OSError) as error:
        raise ValueError('Install requirements-audio.txt to use the audio station') from error
    return np, sd


def list_devices():
    _, sd = _dependencies()
    try:
        devices = sd.query_devices()
    except Exception as error:
        raise ValueError(f'Cannot enumerate audio devices: {error}') from error
    return [{'index': index, 'name': device['name'], 'hostapi': device['hostapi'],
             'input_channels': device['max_input_channels'], 'output_channels': device['max_output_channels'],
             'default_sample_rate': device['default_samplerate']}
            for index, device in enumerate(devices)]


def _write_pcm(path, samples, sample_rate):
    with wave.open(str(path), 'wb') as audio:
        channels = samples.shape[1] if samples.ndim == 2 else 1
        audio.setparams((channels, 2, sample_rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(samples.astype('<i2', copy=False).tobytes())


def capture_fixed(stimulus_path, output_root, input_device, output_device,
                  input_channels=1, output_channels=1, pre_roll_ms=500, tail_ms=5000):
    """Explicit device selection, bounded capture, raw driver times, no correction."""
    np, sd = _dependencies()
    if type(input_device) is not int or type(output_device) is not int or min(input_device, output_device) < 0:
        raise ValueError('Explicit nonnegative audio input/output device indices are required')
    if input_channels not in (1, 2) or output_channels not in (1, 2):
        raise ValueError('Initial station supports mono/stereo input/output')
    if type(pre_roll_ms) is not int or type(tail_ms) is not int or not (0 <= pre_roll_ms <= 10000 and 0 <= tail_ms <= 60000):
        raise ValueError('Pre-roll/tail must be integer milliseconds within 10000/60000')
    source_hash = digest(stimulus_path)
    qa, raw = inspect_audio(stimulus_path, 600000)
    if digest(stimulus_path) != source_hash:
        raise ValueError('Source changed during preparation; freeze the stimulus before capture')
    sample_rate = 16000
    stimulus = np.frombuffer(raw, dtype='<i2')
    pre_samples, tail_samples = pre_roll_ms * 16, tail_ms * 16
    total = pre_samples + len(stimulus) + tail_samples
    if total > sample_rate * 600:
        raise ValueError('Station MVP supports at most 10 minutes including pre-roll/tail')
    try:
        sd.check_input_settings(device=input_device, channels=input_channels, dtype='int16', samplerate=sample_rate)
        sd.check_output_settings(device=output_device, channels=output_channels, dtype='int16', samplerate=sample_rate)
        input_info = dict(sd.query_devices(input_device))
        output_info = dict(sd.query_devices(output_device))
    except Exception as error:
        raise ValueError(f'Audio device preflight failed: {error}') from error
    # Allocate buffers before the callback. No disk writes or log formatting in callback.
    playback = np.zeros((total, output_channels), dtype=np.int16)
    playback[pre_samples:pre_samples + len(stimulus), :] = stimulus[:, None]
    recorded = np.zeros((total, input_channels), dtype=np.int16)
    # Fixed 256-frame callback; capacity includes padding for unusual hosts.
    timing = np.zeros((math.ceil(total / 256) + 32, 6), dtype=np.float64)
    cursor = 0
    blocks = 0
    callback_error = None
    finished = threading.Event()

    def callback(indata, outdata, frames, clock, flags):
        nonlocal cursor, blocks, callback_error
        outdata.fill(0)
        try:
            take = min(frames, total - cursor)
            if blocks >= len(timing) or take <= 0:
                raise RuntimeError('Unexpected callback count or exhausted capture buffer')
            recorded[cursor:cursor + take] = indata[:take]
            outdata[:take] = playback[cursor:cursor + take]
            timing[blocks] = (cursor, take, clock.inputBufferAdcTime, clock.outputBufferDacTime, clock.currentTime, bool(flags))
            blocks += 1
            cursor += take
        except Exception as error:
            callback_error = error
            raise sd.CallbackAbort
        if cursor >= total:
            raise sd.CallbackStop

    session_id = 'CAPTURE-' + uuid.uuid4().hex
    session = Path(output_root) / session_id
    session.mkdir(parents=True, exist_ok=False)
    started_ns = time.monotonic_ns()
    failure = None
    latency = None
    try:
        with sd.Stream(device=(input_device, output_device), channels=(input_channels, output_channels),
                       samplerate=sample_rate, dtype='int16', blocksize=256, callback=callback,
                       finished_callback=finished.set) as stream:
            latency = list(stream.latency)
            if not finished.wait(total / sample_rate + 10):
                failure = 'Audio callback deadline exceeded'
                stream.abort()
    except Exception as error:
        failure = f'Audio stream failed: {error}'
    except KeyboardInterrupt:
        failure = 'Capture interrupted by user'
    if callback_error:
        failure = f'Audio callback failed: {callback_error}'
    rows = timing[:blocks].tolist()
    if any(not math.isfinite(value) for row in rows for value in row):
        failure = 'Nonfinite driver timestamp; timing evidence is unavailable'
        rows = [[value if math.isfinite(value) else None for value in row] for row in rows]
    elif any(rows[index][2] <= rows[index - 1][2] or rows[index][3] <= rows[index - 1][3]
             or rows[index][4] < rows[index - 1][4] for index in range(1, len(rows))):
        failure = 'Driver timestamps do not advance monotonically; timing evidence is unavailable'
    if cursor < total and failure is None:
        failure = 'Capture ended before planned sample count'
    if any(row[5] for row in rows) and failure is None:
        failure = 'Audio overflow/underflow flag: capture is incomplete for timing acceptance'
    files = []
    if cursor:
        _write_pcm(session / 'capture.wav', recorded[:cursor], sample_rate)
        _write_pcm(session / 'stimulus-reference.wav', playback[:cursor, :1], sample_rate)
        files = [{'path': name, 'sha256': digest(session / name), 'sample_rate_hz': sample_rate,
                  'channels': input_channels if name == 'capture.wav' else 1, 'bit_depth': 16,
                  'sample_count': cursor, 'duration_ms': cursor / 16}
                 for name in ('capture.wav', 'stimulus-reference.wav')]
    write_json(session / 'driver-timing.json', {
        'columns': ['first_sample', 'sample_count', 'input_adc_seconds', 'output_dac_seconds', 'callback_seconds', 'xrun'],
        'clock': 'portaudio_stream_monotonic', 'rows': rows,
        'note': 'Driver timestamp estimates; preserve raw values. Not acoustic onset or loopback calibration.'})
    metadata = {'schema_version': '1.0.0', 'session_id': session_id, 'execution_kind': 'hardware',
                'status': 'partial' if failure else 'captured', 'failure': failure,
                'started_monotonic_ns': started_ns, 'ended_monotonic_ns': time.monotonic_ns(),
                'input_device': input_device, 'output_device': output_device,
                'input_device_info': input_info, 'output_device_info': output_info,
                'driver_latency_seconds': latency, 'calibration_applied': False,
                'track_roles': {'capture.wav': 'room_mix_unless_user_verifies_isolation',
                                'stimulus-reference.wav': 'digital_output_reference_not_acoustic_capture'},
                'pre_roll_samples': pre_samples, 'stimulus_samples': len(stimulus),
                'source_sha256': source_hash, 'source_qa': qa, 'artifacts': files,
                'timing_sha256': digest(session / 'driver-timing.json')}
    errors = schema_errors(metadata, 'audio-capture')
    if errors:
        raise ValueError('Invalid capture metadata: ' + '\n'.join(errors))
    write_json(session / 'capture.json', metadata)
    return session, metadata


def loopback_delay(reference, capture, sample_rate, expected_sample=0, max_delay_ms=1000, min_correlation=0.8):
    """Analyze recorded samples. Input provenance is supplied by the caller, never inferred."""
    import numpy as np
    if type(sample_rate) is not int or sample_rate <= 0 or type(expected_sample) is not int or expected_sample < 0:
        raise ValueError('Positive integer sample rate and nonnegative expected sample required')
    if not math.isfinite(max_delay_ms) or not (0 < max_delay_ms <= 5000) or not math.isfinite(min_correlation) or not 0 < min_correlation <= 1:
        raise ValueError('Bounded delay window and correlation threshold in (0,1] required')
    reference = np.asarray(reference, dtype=np.float64)
    capture = np.asarray(capture, dtype=np.float64)
    if reference.ndim != 1 or capture.ndim != 1 or len(reference) < 16 or len(reference) > 16000:
        raise ValueError('Reference must be a mono probe of 16..16000 samples')
    if not np.all(np.isfinite(reference)) or not np.all(np.isfinite(capture)):
        raise ValueError('Nonfinite audio')
    probe = reference - reference.mean()
    energy = float(np.dot(probe, probe))
    if energy <= 0:
        raise ValueError('Constant/silent reference cannot calibrate delay')
    window = capture[expected_sample:expected_sample + int(sample_rate * max_delay_ms / 1000) + len(probe)]
    if len(window) < len(probe):
        return {'status': 'insufficient_evidence', 'delay_ms': None, 'reason': 'Capture shorter than probe'}
    scores = np.correlate(window, probe, mode='valid')
    sums = np.concatenate(([0.0], np.cumsum(window)))
    squares = np.concatenate(([0.0], np.cumsum(window * window)))
    count = len(probe)
    local_energy = squares[count:] - squares[:-count] - (sums[count:] - sums[:-count]) ** 2 / count
    denominator = np.sqrt(np.maximum(local_energy, 0) * energy)
    correlations = np.divide(scores, denominator, out=np.zeros_like(scores), where=denominator > 0)
    best = int(np.argmax(np.abs(correlations)))
    correlation = float(min(1, abs(correlations[best])))
    if correlation < min_correlation:
        return {'status': 'insufficient_evidence', 'delay_ms': None, 'correlation': correlation,
                'reason': 'Probe correlation below configured detection threshold'}
    return {'status': 'observed', 'delay_ms': best * 1000 / sample_rate,
            'delay_samples': best, 'probe_start_sample': expected_sample + best,
            'correlation': correlation, 'polarity': 1 if correlations[best] >= 0 else -1,
            'sample_resolution_ms': 1000 / sample_rate, 'correction_applied': False}


def calibrate_loopback(output_root, input_device, output_device, repetitions=3):
    """Explicit live loopback command; requires the user to connect the test path."""
    np, _ = _dependencies()
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError('Calibration repetitions must be between 1 and 10')
    directory = Path(output_root) / ('CALIBRATION-' + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    probe = np.random.default_rng(20260907).integers(-2000, 2001, size=1024, dtype=np.int16)
    probe_path = directory / 'probe.wav'
    _write_pcm(probe_path, probe, 16000)
    measurements = []
    for _ in range(repetitions):
        session, metadata = capture_fixed(probe_path, directory, input_device, output_device,
                                          pre_roll_ms=500, tail_ms=1500)
        if metadata['status'] != 'captured':
            measurements.append({'session': session.name, 'status': 'insufficient_evidence',
                                 'delay_ms': None, 'reason': metadata['failure']})
            continue
        _, raw = inspect_audio(session / 'capture.wav', 10000)
        signal = np.frombuffer(raw, dtype='<i2')
        result = loopback_delay(probe, signal, 16000, expected_sample=metadata['pre_roll_samples'])
        result['session'] = session.name
        result['delay_basis'] = 'recorded_probe_sample_minus_digital_reference_sample'
        result['capture_sha256'] = digest(session / 'capture.wav')
        timing = load_document(session / 'driver-timing.json')['rows']
        if result['status'] == 'observed':
            output_sample = metadata['pre_roll_samples']
            input_sample = result['probe_start_sample']
            output_row = next(row for row in timing if row[0] <= output_sample < row[0] + row[1])
            input_row = next(row for row in timing if row[0] <= input_sample < row[0] + row[1])
            output_time = output_row[3] + (output_sample - output_row[0]) / 16000
            input_time = input_row[2] + (input_sample - input_row[0]) / 16000
            result['driver_timestamp_path_delay_ms'] = (input_time - output_time) * 1000
            result['driver_timestamp_note'] = 'Driver estimate retained separately; not silently applied to sample delay or DUT metrics'
        measurements.append(result)
    eligible = [item['delay_ms'] for item in measurements if item['status'] == 'observed']
    report = {'schema_version': '1.0.0', 'execution_kind': 'hardware', 'probe_sha256': digest(probe_path),
              'sample_rate_hz': 16000, 'detection_min_correlation': 0.8, 'requested_repetitions': repetitions,
              'sample_count': len(eligible), 'status': 'observed' if len(eligible) == repetitions else 'insufficient_evidence',
              'median_delay_ms': float(np.median(eligible)) if eligible else None,
              'min_delay_ms': min(eligible) if eligible else None, 'max_delay_ms': max(eligible) if eligible else None,
              'correction_applied': False, 'measurements': measurements,
              'note': 'Path includes configured loopback/acoustic route; not internal device latency. Partial calibration is not accepted.'}
    write_json(directory / 'calibration.json', report)
    return directory, report
