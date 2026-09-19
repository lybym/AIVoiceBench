"""Deterministic synthetic audio for the model-VAD tests (offline, no credentials).

A pure sine tone is classified as non-speech by Silero because Silero is trained
on real speech. To exercise a *model* boundary provider without any real
recording, these helpers generate a deterministic speech-*like* signal: a
harmonic glottal source shaped by formant resonances and a syllabic amplitude
envelope. The result is synthetic and is never presented as measured speech; it
only has to be spectro-temporally close enough for the model to produce a
decision the adapter logic can be tested against.

Everything here is seeded and uses only FFT/numpy arithmetic, so the same call
produces byte-identical samples on every run and machine.
"""

import math
from array import array
import sys
import wave

import numpy as np

RATE = 16000

#: Vowel-like formant sets (frequency Hz, bandwidth Hz, gain). Values are
#: textbook resonator placements, not a model of any real speaker.
FORMANTS = {
    'a': ((700.0, 90.0, 1.0), (1220.0, 100.0, 0.7), (2600.0, 140.0, 0.4)),
    'i': ((300.0, 70.0, 1.0), (2200.0, 110.0, 0.6), (3000.0, 150.0, 0.3)),
    'u': ((350.0, 70.0, 1.0), (800.0, 90.0, 0.5), (2400.0, 140.0, 0.2)),
}


def speech_like(duration_ms, f0=110.0, amplitude=0.9, syllable_hz=3.5,
                vowel='a', seed=7, rate=RATE):
    """Deterministic speech-like float32 samples in ``[-1, 1]``."""
    count = int(duration_ms / 1000 * rate)
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    time = np.arange(count) / rate
    source = np.zeros(count)
    harmonic = 1
    while f0 * harmonic < rate / 2 - 100 and harmonic < 80:
        source += (1.0 / harmonic) * np.sin(2 * math.pi * f0 * harmonic * time)
        harmonic += 1
    spectrum = np.fft.rfft(source)
    frequencies = np.fft.rfftfreq(count, 1 / rate)
    shape = np.full_like(frequencies, 0.02)
    for frequency, bandwidth, gain in FORMANTS[vowel]:
        shape += gain / (1 + ((frequencies - frequency) / bandwidth) ** 2)
    signal = np.fft.irfft(spectrum * shape, count)
    envelope = 0.35 + 0.65 * np.abs(np.sin(2 * math.pi * syllable_hz * time))
    edge = max(1, int(0.015 * rate))
    ramp = np.ones(count)
    ramp[:edge] = np.linspace(0, 1, edge)
    ramp[-edge:] = np.linspace(1, 0, edge)
    signal = signal * envelope * ramp
    rng = np.random.default_rng(seed)
    signal = signal + rng.normal(0.0, 0.0015, count)
    peak = float(np.max(np.abs(signal)))
    if peak > 0:
        signal = signal / peak * amplitude
    return signal.astype(np.float32)


def silence(duration_ms, rate=RATE):
    """Zero samples: the exact-silence control."""
    return np.zeros(int(duration_ms / 1000 * rate), dtype=np.float32)


def white_noise(duration_ms, amplitude=0.3, seed=11, rate=RATE):
    """Deterministic white noise: an explicit non-speech *signal*, not silence.

    The legacy energy tests only contrasted a tone with silence. Noise is the
    harder control: it has energy but no speech structure, so a provider that
    merely measures loudness reports a boundary here.
    """
    count = int(duration_ms / 1000 * rate)
    rng = np.random.default_rng(seed)
    return (rng.normal(0.0, amplitude, count)).astype(np.float32)


def tone(duration_ms, frequency=440.0, amplitude=0.35, rate=RATE):
    """A pure sine tone: energetic and unambiguously non-speech."""
    count = int(duration_ms / 1000 * rate)
    time = np.arange(count) / rate
    return (amplitude * np.sin(2 * math.pi * frequency * time)).astype(np.float32)


def concatenate(*sections):
    return np.concatenate([np.asarray(section, dtype=np.float32) for section in sections])


def write_wav(path, samples, rate=RATE, channels=1, sample_width=2):
    """Write float32 samples in ``[-1, 1]`` as canonical PCM16LE mono WAV."""
    clipped = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    ints = np.rint(clipped * 32767.0).astype(np.int16)
    data = array('h', ints.tolist())
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(rate)
        audio.writeframes(data.tobytes())
    return path
