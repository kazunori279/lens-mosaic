export class AudioRecorder {
  constructor(onAudioData) {
    this._onAudioData = onAudioData;
    this._context = null;
    this._worklet = null;
    this._source = null;
    this._stream = null;
  }

  async start() {
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true },
    });

    this._context = new AudioContext({ sampleRate: 16000 });
    await this._context.audioWorklet.addModule("/static/js/pcm-recorder-processor.js");

    this._source = this._context.createMediaStreamSource(this._stream);
    this._worklet = new AudioWorkletNode(this._context, "pcm-recorder-processor");

    this._worklet.port.onmessage = (e) => {
      const float32 = e.data.audio;
      const int16 = new Int16Array(float32.length);
      for (let i = 0; i < float32.length; i++) {
        const s = Math.max(-1, Math.min(1, float32[i]));
        int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this._onAudioData(int16.buffer);
    };

    this._source.connect(this._worklet);
    this._worklet.connect(this._context.destination);
  }

  stop() {
    if (this._worklet) {
      this._worklet.disconnect();
      this._worklet = null;
    }
    if (this._source) {
      this._source.disconnect();
      this._source = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach((t) => t.stop());
      this._stream = null;
    }
    if (this._context) {
      this._context.close();
      this._context = null;
    }
  }
}
