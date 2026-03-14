export class AudioPlayer {
  constructor() {
    this._context = null;
    this._worklet = null;
  }

  async init() {
    this._context = new AudioContext({ sampleRate: 24000 });
    await this._context.audioWorklet.addModule("/static/js/pcm-player-processor.js?v=2");
    this._worklet = new AudioWorkletNode(this._context, "pcm-player-processor");
    this._worklet.connect(this._context.destination);
  }

  play(pcmBytes) {
    if (!this._worklet) return;
    if (this._context.state === "suspended") {
      this._context.resume();
    }
    // Send raw ArrayBuffer — processor converts Int16 to Float32
    this._worklet.port.postMessage(pcmBytes.buffer || pcmBytes);
  }

  stop() {
    if (this._worklet) {
      this._worklet.port.postMessage({ command: "endOfAudio" });
    }
    if (this._context) {
      this._context.close();
      this._context = null;
      this._worklet = null;
    }
  }
}
