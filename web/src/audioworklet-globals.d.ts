/* AIVoiceBench - ambient types for the AudioWorklet global scope.
 *
 * The TypeScript `DOM` library describes the *window* global scope. An
 * AudioWorkletProcessor module runs in a separate global scope that has no DOM:
 * it gets `AudioWorkletProcessor`, `registerProcessor` and `sampleRate` instead.
 * These declarations add only what the PCM capture worklet actually uses, so the
 * worklet can be type checked without pulling in a third-party AudioWorklet
 * type package (and without loosening any strictness option).
 *
 * This file must not emit any JavaScript: it is types only.
 */

interface AudioWorkletProcessorOptions {
  numberOfInputs?: number;
  numberOfOutputs?: number;
  outputChannelCount?: number[];
  processorOptions?: unknown;
}

interface AudioWorkletProcessorPort {
  postMessage(message: unknown, transfer?: Transferable[]): void;
  onmessage: ((event: MessageEvent) => void) | null;
}

/** Parameters passed to `registerProcessor` by `audioWorklet.addModule()` callers. */
declare class AudioWorkletProcessor {
  constructor(options?: AudioWorkletProcessorOptions);
  readonly port: AudioWorkletProcessorPort;
}

/** The rate of the AudioContext that owns this worklet (AudioWorklet global scope). */
declare const sampleRate: number;

/** Register a processor class under the name used by `new AudioWorkletNode()`. */
declare function registerProcessor(
  name: string,
  processorCtor: new (options?: AudioWorkletProcessorOptions) => AudioWorkletProcessor,
): void;