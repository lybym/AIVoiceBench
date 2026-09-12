/* AIVoiceBench — PCM capture worklet for Streaming ASR (PRD-F023).
 *
 * Turns the microphone stream into the format the streaming recogniser expects:
 * mono, 16-bit signed little-endian PCM at the target sample rate.
 *
 * The browser's AudioContext may not run at the target rate, so the worklet
 * resamples with linear interpolation instead of silently sending audio at a
 * rate the recogniser was not configured for. Frames are posted to the main
 * thread as transferable Int16Array buffers; the main thread adds the sequence
 * header and forwards them to the backend.
 *
 * This file is loaded with audioWorklet.addModule('/static/pcm_capture_worklet.js')
 * and is plain JS (no bundler); `sampleRate` is provided by the AudioWorklet
 * global scope.
 */

class PCMCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.frameSamples = opts.frameSamples || 3200; // 200 ms at 16 kHz
    this.ratio = sampleRate / this.targetRate;
    this.position = 0; // fractional read position carried across blocks
    this.previous = 0; // last input sample of the previous block
    this.buffer = new Int16Array(this.frameSamples);
    this.filled = 0;
    this.stopped = false;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'stop') {
        this.stopped = true;
        this.flush(true);
      }
    };
  }

  flush(final) {
    if (this.filled === 0) {
      if (final) this.port.postMessage({ type: 'stopped' });
      return;
    }
    const chunk = this.buffer.slice(0, this.filled);
    this.port.postMessage({
      type: 'frame',
      samples: this.filled,
      pcm: chunk.buffer,
    }, [chunk.buffer]);
    this.buffer = new Int16Array(this.frameSamples);
    this.filled = 0;
    if (final) this.port.postMessage({ type: 'stopped' });
  }

  push(value) {
    const clamped = value < -1 ? -1 : value > 1 ? 1 : value;
    this.buffer[this.filled++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (this.filled === this.frameSamples) this.flush(false);
  }

  process(inputs) {
    if (this.stopped) return false;
    const input = inputs[0];
    if (!input || !input.length || !input[0] || !input[0].length) return true;

    const blockLength = input[0].length;
    const channels = input.length;
    // Downmix to mono first: the recogniser consumes one channel.
    const mono = new Float32Array(blockLength);
    for (let channel = 0; channel < channels; channel += 1) {
      const data = input[channel];
      for (let i = 0; i < blockLength; i += 1) mono[i] += data[i];
    }
    if (channels > 1) {
      for (let i = 0; i < blockLength; i += 1) mono[i] /= channels;
    }

    let position = this.position;
    while (position < blockLength) {
      const index = Math.floor(position);
      const fraction = position - index;
      const before = index === 0 ? this.previous : mono[index - 1];
      const after = mono[index];
      this.push(before + (after - before) * fraction);
      position += this.ratio;
    }
    this.previous = mono[blockLength - 1];
    this.position = position - blockLength;
    return true;
  }
}

registerProcessor('pcm-capture', PCMCaptureProcessor);
