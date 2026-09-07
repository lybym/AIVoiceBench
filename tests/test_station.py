import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

try:
    import numpy as np
except ImportError:
    np = None

from aivoicebench.station import capture_fixed, loopback_delay
from aivoicebench.validation import load_document


class StopCallback(Exception):
    pass


class AbortCallback(Exception):
    pass


class FakeAudio:
    """Software callback harness only. Never opens hardware."""
    CallbackStop = StopCallback
    CallbackAbort = AbortCallback

    def __init__(self, flags=False, fail_open=False, fail_callback=False):
        self.flags, self.fail_open, self.fail_callback = flags, fail_open, fail_callback

    def query_devices(self, index):
        return dict(name='SYNTHETIC TEST DEVICE', max_input_channels=2, max_output_channels=2, hostapi=0, default_samplerate=16000)

    def check_input_settings(self, **kwargs):
        pass

    def check_output_settings(self, **kwargs):
        pass

    def Stream(self, **kwargs):
        owner = self
        class Stream:
            latency = (0.01, 0.02)

            def __enter__(self):
                if owner.fail_open:
                    raise RuntimeError('simulated device unplug')
                cursor = 0
                while True:
                    channels_in, channels_out = kwargs['channels']
                    indata = np.full((256, channels_in), 123, dtype=np.int16)
                    outdata = np.empty((256, channels_out), dtype=np.int16)
                    clock = SimpleNamespace(inputBufferAdcTime=10 + cursor / 16000,
                        outputBufferDacTime=10.01 + cursor / 16000, currentTime=10.02 + cursor / 16000)
                    if owner.fail_callback:
                        indata = np.zeros((1, 3), dtype=np.int16)
                    try:
                        kwargs['callback'](indata, outdata, 256, clock, owner.flags)
                    except (StopCallback, AbortCallback):
                        kwargs['finished_callback']()
                        break
                    cursor += 256
                    if cursor > 16000 * 10:
                        raise RuntimeError('Test callback did not terminate')
                return self

            def __exit__(self, *args):
                pass

            def abort(self):
                pass
        return Stream()


@unittest.skipIf(np is None, 'Optional numpy not installed; install requirements-audio.txt for audio checks')
class StationSoftwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes((1000).to_bytes(2, 'little', signed=True) * 1600)

    def capture(self, backend, **kwargs):
        with patch('aivoicebench.station._dependencies', return_value=(np, backend)):
            return capture_fixed(self.source, self.root / 'output', 0, 1, pre_roll_ms=100, tail_ms=100, **kwargs)

    def test_callback_capture_metadata_and_stereo(self):
        path, metadata = self.capture(FakeAudio(), input_channels=2)
        self.assertEqual(metadata['status'], 'captured')
        self.assertFalse(metadata['calibration_applied'])
        with wave.open(str(path / 'capture.wav'), 'rb') as audio:
            self.assertEqual(audio.getnchannels(), 2)
            self.assertEqual(audio.getnframes(), 4800)
        with wave.open(str(path / 'stimulus-reference.wav'), 'rb') as audio:
            samples = np.frombuffer(audio.readframes(4800), dtype='<i2')
        self.assertTrue(np.all(samples[:1600] == 0))
        self.assertTrue(np.all(samples[1600:3200] == 1000))
        self.assertTrue(np.all(samples[3200:] == 0))
        rows = load_document(path / 'driver-timing.json')['rows']
        self.assertEqual(rows[0][2], 10)
        self.assertEqual(rows[0][3], 10.01)
        self.assertEqual(sum(row[1] for row in rows), 4800)

    def test_xrun_is_partial(self):
        _, metadata = self.capture(FakeAudio(flags=True))
        self.assertEqual(metadata['status'], 'partial')
        self.assertIn('overflow/underflow', metadata['failure'])

    def test_stream_failure_persists_reason(self):
        path, metadata = self.capture(FakeAudio(fail_open=True))
        self.assertEqual(metadata['status'], 'partial')
        self.assertEqual(metadata['artifacts'], [])
        self.assertTrue((path / 'capture.json').exists())
        self.assertIn('simulated device unplug', metadata['failure'])

    def test_callback_exception_is_not_lost(self):
        _, metadata = self.capture(FakeAudio(fail_callback=True))
        self.assertEqual(metadata['status'], 'partial')
        self.assertIn('callback failed', metadata['failure'])

    def test_explicit_device_selection_required(self):
        with patch('aivoicebench.station._dependencies', return_value=(np, FakeAudio())):
            with self.assertRaisesRegex(ValueError, 'Explicit'):
                capture_fixed(self.source, self.root, None, 1)

    def test_exact_loopback_delay_with_gain_and_polarity(self):
        probe = np.random.default_rng(42).normal(size=1024)
        for gain in (0.25, -0.5):
            signal = np.concatenate((np.zeros(800), gain * probe, np.zeros(1000)))
            result = loopback_delay(probe, signal, 16000)
            self.assertEqual(result['status'], 'observed')
            self.assertEqual(result['delay_samples'], 800)
            self.assertEqual(result['delay_ms'], 50)
            self.assertEqual(result['polarity'], 1 if gain > 0 else -1)
            self.assertFalse(result['correction_applied'])

    def test_no_probe_no_delay(self):
        probe = np.random.default_rng(42).normal(size=1024)
        result = loopback_delay(probe, np.zeros(4000), 16000)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['delay_ms'])

    def test_loopback_noise_and_pre_roll(self):
        rng = np.random.default_rng(7)
        probe = rng.normal(size=1024)
        signal = rng.normal(scale=0.01, size=8000)
        signal[2400:3424] += probe
        result = loopback_delay(probe, signal, 16000, expected_sample=1600)
        self.assertEqual(result['delay_ms'], 50)

    def test_bad_calibration_inputs(self):
        with self.assertRaisesRegex(ValueError, 'Constant'):
            loopback_delay(np.zeros(100), np.zeros(1000), 16000)
        with self.assertRaisesRegex(ValueError, 'Nonfinite'):
            loopback_delay(np.arange(100), [float('nan')] * 1000, 16000)
        with self.assertRaises(ValueError):
            loopback_delay(np.arange(100), np.zeros(1000), 0)


if __name__ == '__main__':
    unittest.main()
