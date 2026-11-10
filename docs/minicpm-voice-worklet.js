const FRAME_SAMPLES = 320; // 20 ms at 16 kHz.
const PRE_ROLL_FRAMES = 8;
const SPEECH_START_FRAMES = 6;
const SPEECH_END_FRAMES = 25;
const ECHO_DOWNSAMPLE = 4;
const ECHO_FRAME_SAMPLES = FRAME_SAMPLES / ECHO_DOWNSAMPLE;
const ECHO_DELAY_SAMPLES = 1200; // Search up to 300 ms of device and acoustic delay.

class MiniCPMCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Float32Array(FRAME_SAMPLES);
    this.referenceFrame = new Float32Array(FRAME_SAMPLES);
    this.echoReference = new Float32Array(ECHO_DELAY_SAMPLES + ECHO_FRAME_SAMPLES);
    this.echoMic = new Float32Array(ECHO_FRAME_SAMPLES);
    this.echoOffset = 0;
    this.echoAge = ECHO_DELAY_SAMPLES;
    this.frameOffset = 0;
    this.history = new Float32Array(PRE_ROLL_FRAMES * FRAME_SAMPLES);
    this.keepHistory = new Uint8Array(PRE_ROLL_FRAMES);
    this.historyIndex = 0;
    this.chunk = new Float32Array(16000);
    this.chunkOffset = 0;
    this.noiseFloor = 0.003;
    this.speaking = false;
    this.activityFrames = 0;
  }

  isPlaybackEcho(level, threshold) {
    const reference = this.echoReference;
    let micSum = 0;
    let micEnergy = 0;
    let hasReference = false;
    for (let i = 0; i < ECHO_FRAME_SAMPLES; i++) {
      const j = i * ECHO_DOWNSAMPLE;
      const mic = (this.frame[j] + this.frame[j + 1] + this.frame[j + 2] + this.frame[j + 3]) / ECHO_DOWNSAMPLE;
      const sample = (this.referenceFrame[j] + this.referenceFrame[j + 1] + this.referenceFrame[j + 2] + this.referenceFrame[j + 3]) / ECHO_DOWNSAMPLE;
      this.echoMic[i] = mic;
      micSum += mic;
      micEnergy += mic * mic;
      reference[this.echoOffset] = sample;
      this.echoOffset = (this.echoOffset + 1) % reference.length;
      hasReference ||= Math.abs(sample) > 0.001;
    }
    this.echoAge = hasReference ? 0 : this.echoAge + ECHO_FRAME_SAMPLES;
    if (this.echoAge > ECHO_DELAY_SAMPLES || level < 0.006) return false;

    const micVariance = micEnergy - micSum * micSum / ECHO_FRAME_SAMPLES;
    if (micVariance <= 0) return false;
    // Independent near-end speech survives when the playback projection cannot explain its energy.
    // Allow some distortion from room acoustics and sub-sample resampling on loud playback.
    const minimumMatch = Math.min(0.92, Math.max(0.6, 1 - (threshold / level) ** 2));
    let start = this.echoOffset;
    for (let delay = ECHO_DELAY_SAMPLES; delay >= 0; delay--) {
      let index = start;
      let refSum = 0;
      let refEnergy = 0;
      let dot = 0;
      for (const mic of this.echoMic) {
        const sample = reference[index];
        if (++index === reference.length) index = 0;
        refSum += sample;
        refEnergy += sample * sample;
        dot += sample * mic;
      }
      const refVariance = refEnergy - refSum * refSum / ECHO_FRAME_SAMPLES;
      const covariance = dot - micSum * refSum / ECHO_FRAME_SAMPLES;
      if (refVariance > 0.00008 && covariance * covariance >= minimumMatch * micVariance * refVariance) return true;
      if (++start === reference.length) start = 0;
    }
    return false;
  }

  updateSpeech() {
    let sum = 0;
    let energy = 0;
    for (const sample of this.frame) {
      sum += sample;
      energy += sample * sample;
    }
    const mean = sum / FRAME_SAMPLES;
    let level = Math.sqrt(Math.max(0, energy / FRAME_SAMPLES - mean * mean));
    // A browser microphone with echo cancellation can deliver a quiet, close-mic question.
    // Keep the floor above normal room noise while allowing that question to interrupt reading.
    const startThreshold = Math.max(0.008, this.noiseFloor * 2.4);
    if (this.isPlaybackEcho(level, startThreshold)) {
      // Do not send playback echo, including via pre-roll after a genuine question starts.
      this.frame.fill(0);
      level = 0;
    }
    if (this.speaking) {
      const quiet = level < Math.max(0.006, this.noiseFloor * 1.8);
      this.activityFrames = quiet ? this.activityFrames + 1 : 0;
      if (this.activityFrames === SPEECH_END_FRAMES) {
        this.speaking = false;
        this.activityFrames = 0;
        this.port.postMessage({ type: 'speech_end' });
      }
      return;
    }
    if (level >= startThreshold) {
      this.activityFrames++;
      if (this.activityFrames === SPEECH_START_FRAMES) {
        this.speaking = true;
        this.activityFrames = 0;
        this.keepHistory.fill(1);
        this.port.postMessage({ type: 'speech_start' });
      }
    } else {
      this.activityFrames = 0;
      this.noiseFloor += 0.05 * (level - this.noiseFloor);
    }
  }

  appendFrame() {
    this.updateSpeech();
    // Delay by the pre-roll length so an onset can recover audio across a chunk boundary.
    const historyOffset = this.historyIndex * FRAME_SAMPLES;
    if (this.keepHistory[this.historyIndex]) {
      this.chunk.set(this.history.subarray(historyOffset, historyOffset + FRAME_SAMPLES), this.chunkOffset);
    }
    this.history.set(this.frame, historyOffset);
    this.keepHistory[this.historyIndex] = Number(this.speaking);
    this.historyIndex = (this.historyIndex + 1) % PRE_ROLL_FRAMES;
    this.chunkOffset += FRAME_SAMPLES;
    if (this.chunkOffset === this.chunk.length) {
      this.port.postMessage({ type: 'chunk', audio: this.chunk.buffer }, [this.chunk.buffer]);
      this.chunk = new Float32Array(16000);
      this.chunkOffset = 0;
    }
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    const reference = inputs[1]?.[0];
    let start = 0;
    while (start < input.length) {
      const count = Math.min(input.length - start, FRAME_SAMPLES - this.frameOffset);
      this.frame.set(input.subarray(start, start + count), this.frameOffset);
      if (reference) this.referenceFrame.set(reference.subarray(start, start + count), this.frameOffset);
      else this.referenceFrame.fill(0, this.frameOffset, this.frameOffset + count);
      this.frameOffset += count;
      start += count;
      if (this.frameOffset === FRAME_SAMPLES) {
        this.appendFrame();
        this.frameOffset = 0;
      }
    }
    return true;
  }
}

registerProcessor('minicpm-capture', MiniCPMCapture);
