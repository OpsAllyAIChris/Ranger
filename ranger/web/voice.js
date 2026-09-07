/**
 * Tier 7d. The microphone and the loudspeaker, in the browser.
 *
 * Two halves that never touch each other. Capture records one bounded
 * utterance and hands back a blob. Playback takes sentences of audio as they
 * arrive, plays them in order, and reports the amplitude of what is actually
 * coming out of the speaker.
 *
 * That last part is the whole reason the audio lives here. The orb's one input
 * is how loud Ranger is right now, and the only place that is really known is
 * where the sound is being made. Measuring it in Python and sending a number
 * over a socket would be animating a guess about something happening somewhere
 * else.
 *
 * Nothing here decides anything. It records when told, plays what it is given,
 * and reports numbers.
 */

/** Chrome gives webm/opus. The list is ordered by what Deepgram reads best. */
const FORMATS = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

/** Below this the recording is a click or a breath, not a sentence. */
const MIN_SECONDS = 0.25;

/** A latched microphone that is forgotten about should not run all day. */
const MAX_SECONDS = 120;

export function supported() {
  return Boolean(
    navigator.mediaDevices &&
    navigator.mediaDevices.getUserMedia &&
    window.MediaRecorder
  );
}

function pickFormat() {
  for (const format of FORMATS) {
    if (window.MediaRecorder.isTypeSupported(format)) return format;
  }
  return '';
}

/**
 * The microphone. One stream, kept open between utterances so the browser does
 * not show its recording indicator flickering on and off, and so the second
 * utterance does not pay for permission and device startup again.
 */
export function createMicrophone({ onLevel, onError } = {}) {
  let stream = null;
  let recorder = null;
  let chunks = [];
  let startedAt = 0;
  let meter = null;
  let stopTimer = null;

  async function open() {
    if (stream) return stream;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (onLevel) meter = createMeter(stream, onLevel);
    return stream;
  }

  /** Start recording. Resolves once the recorder is actually running. */
  async function start() {
    if (recorder && recorder.state === 'recording') return;
    await open();

    const mimeType = pickFormat();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onerror = (e) => onError && onError(e.error || e);
    recorder.start();
    startedAt = performance.now();

    clearTimeout(stopTimer);
    stopTimer = setTimeout(() => {
      // A latch that was forgotten. Stopping is better than a recording nobody
      // meant to make, and the caller finds out because stop() resolves.
      if (recorder && recorder.state === 'recording') recorder.stop();
    }, MAX_SECONDS * 1000);
  }

  /**
   * Stop and hand back what was recorded, or null if it was too short to be
   * anything. Resolves only once the recorder has flushed: reading `chunks`
   * before onstop loses the tail of the sentence.
   */
  function stop() {
    clearTimeout(stopTimer);
    return new Promise((resolve) => {
      if (!recorder || recorder.state !== 'recording') {
        resolve(null);
        return;
      }
      const seconds = (performance.now() - startedAt) / 1000;
      const type = recorder.mimeType || 'audio/webm';
      recorder.onstop = () => {
        const blob = new Blob(chunks, { type });
        chunks = [];
        if (seconds < MIN_SECONDS || blob.size < 512) {
          resolve(null);
          return;
        }
        resolve({ blob, seconds, mime: type });
      };
      recorder.stop();
    });
  }

  function close() {
    clearTimeout(stopTimer);
    if (meter) { meter.stop(); meter = null; }
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
      stream = null;
    }
    recorder = null;
  }

  return {
    start,
    stop,
    close,
    get recording() { return Boolean(recorder && recorder.state === 'recording'); },
  };
}

/** Input level while recording, so the mic button can show it is hearing you. */
function createMeter(stream, onLevel) {
  const context = new (window.AudioContext || window.webkitAudioContext)();
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  source.connect(analyser);

  const buffer = new Float32Array(analyser.fftSize);
  let running = true;

  function tick() {
    if (!running) return;
    analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i++) sum += buffer[i] * buffer[i];
    onLevel(Math.min(1, Math.sqrt(sum / buffer.length) * 4));
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  return {
    stop() {
      running = false;
      try { source.disconnect(); } catch (e) { /* already gone */ }
      context.close().catch(() => {});
    },
  };
}

/**
 * The loudspeaker. Sentences arrive one at a time and have to play in order
 * with no gap, so each is scheduled against a running playhead rather than
 * started when it happens to arrive.
 */
export function createSpeaker({ onLevel, onDone } = {}) {
  let context = null;
  let analyser = null;
  let playhead = 0;
  let playing = 0;
  let watching = false;
  let sources = [];

  function ensure() {
    if (context) return context;
    context = new (window.AudioContext || window.webkitAudioContext)();
    analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    analyser.connect(context.destination);
    return context;
  }

  function watch() {
    if (watching || !onLevel) return;
    watching = true;
    const buffer = new Float32Array(analyser.fftSize);
    const tick = () => {
      if (!watching) return;
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) sum += buffer[i] * buffer[i];
      // Root mean square, scaled so ordinary speech uses most of the range.
      // Not peak: peak sits at the top through a whole sentence and the orb
      // stops saying anything about how it is being said.
      onLevel(Math.min(1, Math.sqrt(sum / buffer.length) * 3.2));
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  /**
   * Queue one sentence. Bytes in, scheduled immediately after the last one.
   *
   * `format` says how to read them, because for the default output the bytes
   * cannot say for themselves: pcm_24000 is 16 bit little endian samples with
   * no header of any kind, and decodeAudioData rejects it. That is not an
   * ElevenLabs problem or an autoplay problem, it is the absence of a
   * container, and the same bytes play perfectly through PortAudio because
   * that path is told the rate separately.
   */
  async function play(bytes, format) {
    const audio = ensure();
    // Autoplay policy: a context created before any click starts suspended,
    // and a resume inside the click that started this is what unlocks it.
    if (audio.state === 'suspended') await audio.resume();

    const buffer = (format && format.encoding === 'pcm')
      ? pcmBuffer(audio, bytes, format)
      : await audio.decodeAudioData(bytes.slice(0));
    const source = audio.createBufferSource();
    source.buffer = buffer;
    source.connect(analyser);

    const now = audio.currentTime;
    const at = Math.max(now, playhead);
    source.start(at);
    playhead = at + buffer.duration;

    playing += 1;
    sources.push(source);
    watch();
    source.onended = () => {
      playing -= 1;
      sources = sources.filter((item) => item !== source);
      if (playing <= 0) {
        watching = false;
        if (onLevel) onLevel(0);
        if (onDone) onDone();
      }
    };
  }

  /** Barge-in. Everything queued stops now and the playhead is reset. */
  function stop() {
    for (const source of sources) {
      try { source.stop(); } catch (e) { /* already ended */ }
    }
    sources = [];
    playing = 0;
    watching = false;
    playhead = context ? context.currentTime : 0;
    if (onLevel) onLevel(0);
  }

  /** Unlock the audio context from inside a click, before there is anything to play. */
  async function unlock() {
    const audio = ensure();
    if (audio.state === 'suspended') await audio.resume();
  }

  return { play, stop, unlock, get speaking() { return playing > 0; } };
}

/** Raw 16 bit little endian samples into something the graph can play. */
function pcmBuffer(audio, bytes, format) {
  const rate = format.rate || 24000;
  const channels = format.channels || 1;
  const view = new DataView(bytes);
  // Floor, because a sentence can end mid sample when chunks are concatenated.
  const frames = Math.floor(view.byteLength / 2 / channels);
  const buffer = audio.createBuffer(channels, frames, rate);

  for (let channel = 0; channel < channels; channel++) {
    const target = buffer.getChannelData(channel);
    for (let i = 0; i < frames; i++) {
      // 32768 rather than 32767: it is the magnitude of the most negative
      // sample, so nothing can come out above 1.0 and clip.
      target[i] = view.getInt16((i * channels + channel) * 2, true) / 32768;
    }
  }
  return buffer;
}

/** A blob to the base64 the socket carries. */
export function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const result = String(reader.result);
      resolve(result.slice(result.indexOf(',') + 1));
    };
    reader.readAsDataURL(blob);
  });
}

/** Base64 from the socket back to bytes the audio context can decode. */
export function fromBase64(text) {
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}
