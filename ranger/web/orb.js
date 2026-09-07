/**
 * Tier 7a. The orb and the cosmic background.
 *
 * This module is the whole scene and nothing else. It has one input, a number
 * between 0 and 1, and it draws. It does not know what a websocket is, what a
 * turn is, or what Ranger is for. Amendment A says no agent logic in the
 * browser; this file is the far end of that rule, where there is not even any
 * interface logic.
 *
 * The one input is `setVoiceBright`. Tier 7b will feed it playback amplitude
 * over the transport. Until then `?demo` sweeps it so there is something to
 * look at.
 *
 * Three layers glow: a wide atmosphere, a medium halo, a bright inner core.
 * Their base colours are deliberately restrained. The bloom pass is what makes
 * it glow, and brightening the materials instead gives a flat disc.
 */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

const BG = 0x0e0f13;
const ACCENT = '#2DD4A8';
const CORE = '#B9FFEC';

const PULSE_SECONDS = 4.0;

// Attack fast, release slow. Raw amplitude arrives in bursts at syllable rate,
// and following it exactly makes the orb strobe. Rising quickly keeps speech
// feeling immediate; falling slowly stops every gap between words reading as a
// stop. Both are time constants in seconds, applied frame rate independently,
// so a 144Hz screen and a 30fps one settle at the same speed.
const ATTACK_SECONDS = 0.045;
const RELEASE_SECONDS = 0.22;

// A very wide, very shallow gradient on a near black background quantises into
// visible concentric rings at 8 bits per channel. A sub-quantum of noise breaks
// the contours up; without it the atmosphere layer reads as a stack of discs.
const DITHER = `
  float dither(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453) - 0.5;
  }
`;

const NOISE = `
  vec2 hash2(vec2 p) {
    p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
    return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
  }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(dot(hash2(i + vec2(0.0, 0.0)), f - vec2(0.0, 0.0)),
                   dot(hash2(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0)), u.x),
               mix(dot(hash2(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0)),
                   dot(hash2(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0)), u.x), u.y);
  }
  float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.02; a *= 0.5; }
    return v;
  }
`;

const GLOW_VERT = `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }
`;

const GLOW_FRAG = `
  precision highp float;
  varying vec2 vUv;
  uniform vec3  uColor;
  uniform float uPower;       // falloff sharpness
  uniform float uIntensity;   // base brightness
  uniform float uVoice;       // how much uVoiceBright lifts this layer
  uniform float uVoiceBright; // 0..1, shared by every layer
  uniform float uPulse;       // the idle breath
  ${DITHER}
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    // Jitter the radius rather than the colour: the layers are additive, so
    // noise added after the alpha multiply is scaled away exactly where the
    // banding is worst.
    d += dither(gl_FragCoord.xy) * 0.004;
    // smoothstep, not a bare 1-d ramp: its slope is zero at both ends, so the
    // layer reaches nothing at the edge of its plane instead of stopping at a
    // small non-zero value. On a background this dark that difference is a
    // visible ring around the widest layer.
    float a = pow(smoothstep(1.0, 0.0, d), uPower);
    float lift = 1.0 + uVoice * uVoiceBright + uPulse;
    gl_FragColor = vec4(uColor * a * uIntensity * lift, a * min(1.0, uIntensity * lift));
  }
`;

/**
 * Build the scene on a canvas and start drawing.
 *
 * Returns the seam, and only the seam. Everything the rest of Tier 7 is
 * allowed to do to the orb is on this object.
 */
export function createOrb(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(BG, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 200);
  camera.position.z = 14;

  // One clock, one shared brightness. Everything that glows reads these.
  const uniforms = {
    uTime: { value: 0 },
    uVoiceBright: { value: 0 },
  };

  // ------------------------------------------------------------- nebula
  // Layered fractal noise rather than radial gradients: gradients band badly
  // on a background this dark, and noise drifts without ever repeating.
  const nebula = new THREE.Mesh(
    new THREE.PlaneGeometry(2, 2),
    new THREE.ShaderMaterial({
      depthTest: false,
      depthWrite: false,
      uniforms: {
        uTime: uniforms.uTime,
        uAspect: { value: 1 },
        // The nebula plane is opaque and covers the frame, so it decides the
        // background colour. Without this the corners go to pure black
        // instead of #0E0F13 and the design token is quietly lost.
        uBase: { value: new THREE.Color(BG) },
      },
      vertexShader: `
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }
      `,
      fragmentShader: `
        precision highp float;
        varying vec2 vUv;
        uniform float uTime;
        uniform float uAspect;
        uniform vec3  uBase;
        ${NOISE}
        ${DITHER}
        void main() {
          vec2 p = (vUv - 0.5) * vec2(uAspect, 1.0) * 3.0;
          float t = uTime * 0.012;

          // Three wisps drifting at different rates so they never lock up.
          float teal   = fbm(p * 1.1 + vec2( t * 1.7,  t * 0.9));
          float purple = fbm(p * 0.8 + vec2(-t * 1.1,  t * 1.4) + 11.0);
          float blue   = fbm(p * 1.4 + vec2( t * 0.6, -t * 1.2) + 23.0);

          // Same thresholds for all three. Giving teal a lower floor let it
          // cover the other two, and the result was one green cloud rather
          // than three colours drifting through each other.
          vec3 col = vec3(0.0);
          col += vec3(0.176, 0.831, 0.659) * smoothstep(0.12, 0.68, teal)   * 0.13;
          col += vec3(0.45,  0.30,  0.92)  * smoothstep(0.12, 0.68, purple) * 0.20;
          col += vec3(0.20,  0.42,  0.92)  * smoothstep(0.12, 0.68, blue)   * 0.17;

          // Darker at the edges so the middle of the frame carries the eye.
          float vignette = 1.0 - smoothstep(0.35, 1.15, length(vUv - 0.5) * 2.0);
          vec3 rgb = uBase + col * vignette + dither(gl_FragCoord.xy) / 255.0;
          gl_FragColor = vec4(rgb, 1.0);
        }
      `,
    })
  );
  nebula.frustumCulled = false;
  nebula.renderOrder = -2;
  scene.add(nebula);

  // ----------------------------------------------------------- starfield

  const STAR_COUNT = 1400;
  const starPos = new Float32Array(STAR_COUNT * 3);
  const starPhase = new Float32Array(STAR_COUNT);
  const starSize = new Float32Array(STAR_COUNT);
  for (let i = 0; i < STAR_COUNT; i++) {
    starPos[i * 3 + 0] = (Math.random() - 0.5) * 90;
    starPos[i * 3 + 1] = (Math.random() - 0.5) * 60;
    starPos[i * 3 + 2] = -12 - Math.random() * 55;
    starPhase[i] = Math.random() * Math.PI * 2;
    starSize[i] = 0.6 + Math.random() * 1.9;
  }
  const starGeo = new THREE.BufferGeometry();
  starGeo.setAttribute('position', new THREE.BufferAttribute(starPos, 3));
  starGeo.setAttribute('aPhase', new THREE.BufferAttribute(starPhase, 1));
  starGeo.setAttribute('aSize', new THREE.BufferAttribute(starSize, 1));

  const stars = new THREE.Points(starGeo, new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    uniforms: { uTime: uniforms.uTime, uPixelRatio: { value: renderer.getPixelRatio() } },
    vertexShader: `
      attribute float aPhase;
      attribute float aSize;
      uniform float uTime;
      uniform float uPixelRatio;
      varying float vTwinkle;
      void main() {
        // Slow, out of phase, and never fully off.
        vTwinkle = 0.45 + 0.55 * (0.5 + 0.5 * sin(uTime * 0.9 + aPhase));
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = aSize * uPixelRatio * (70.0 / -mv.z);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      precision highp float;
      varying float vTwinkle;
      void main() {
        float d = length(gl_PointCoord - 0.5) * 2.0;
        float a = pow(max(0.0, 1.0 - d), 2.2) * vTwinkle;
        gl_FragColor = vec4(vec3(0.80, 0.92, 0.98) * a, a);
      }
    `,
  }));
  scene.add(stars);

  // ---------------------------------------------------------------- orb

  const orb = new THREE.Group();
  scene.add(orb);

  const layer = ({ size, color, power, intensity, voice }) => {
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(size, size),
      new THREE.ShaderMaterial({
        transparent: true,
        depthWrite: false,
        depthTest: false,
        blending: THREE.AdditiveBlending,
        uniforms: {
          uColor: { value: new THREE.Color(color) },
          uPower: { value: power },
          uIntensity: { value: intensity },
          uVoice: { value: voice },
          uVoiceBright: uniforms.uVoiceBright,
          uPulse: { value: 0 },
        },
        vertexShader: GLOW_VERT,
        fragmentShader: GLOW_FRAG,
      })
    );
    orb.add(mesh);
    return mesh;
  };

  //                       wide soft atmosphere -> medium halo -> inner core
  const atmosphere = layer({ size: 15.0, color: ACCENT, power: 2.6, intensity: 0.20, voice: 0.35 });
  const halo       = layer({ size:  6.4, color: ACCENT, power: 3.2, intensity: 0.55, voice: 0.90 });
  const core       = layer({ size:  2.6, color: CORE,   power: 4.5, intensity: 0.95, voice: 1.30 });

  // --------------------------------------------------------- post-process

  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 1.55, 0.75, 0.0);
  composer.addPass(bloom);

  // ------------------------------------------------------------- resizing

  function resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    bloom.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    nebula.material.uniforms.uAspect.value = w / h;
  }
  window.addEventListener('resize', resize);
  resize();

  // ---------------------------------------------------------------- loop

  const clock = new THREE.Clock();
  let target = 0;       // what it was told
  let current = 0;      // what it is drawing, chasing target
  let demo = false;
  let running = true;
  let frames = 0;
  let fps = 0;
  let fpsSince = 0;
  let onFrame = null;

  function step() {
    if (!running) return;
    const dt = Math.min(clock.getDelta(), 0.1); // a backgrounded tab must not jump
    const t = clock.getElapsedTime();
    uniforms.uTime.value = t;

    if (demo) {
      // Something to look at without wiring anything up: a slow sweep of the
      // full range, so the top and bottom are both visible.
      target = 0.5 + 0.5 * Math.sin(t * 1.6);
    }

    const tau = target > current ? ATTACK_SECONDS : RELEASE_SECONDS;
    current += (target - current) * (1 - Math.exp(-dt / tau));
    uniforms.uVoiceBright.value = current;

    // Breathing, so it never looks frozen when nobody is talking. Sine over
    // four seconds, fading out as the voice comes up so the two do not fight.
    const idle = (0.5 + 0.5 * Math.sin((t / PULSE_SECONDS) * Math.PI * 2)) *
                 0.16 * (1 - current);
    core.material.uniforms.uPulse.value = idle;
    halo.material.uniforms.uPulse.value = idle * 0.7;
    atmosphere.material.uniforms.uPulse.value = idle * 0.35;

    // A touch of drift so the whole frame is never perfectly still.
    orb.position.x = Math.sin(t * 0.11) * 0.10;
    orb.position.y = Math.cos(t * 0.09) * 0.08;

    composer.render();

    frames += 1;
    if (t - fpsSince >= 0.5) {
      fps = frames / (t - fpsSince);
      frames = 0;
      fpsSince = t;
    }
    if (onFrame) onFrame({ time: t, fps, voiceBright: current, target });

    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);

  return {
    /** The one input. 0 is quiet, 1 is loud. Anything else is clamped. */
    setVoiceBright(value) {
      demo = false;
      const v = Number(value);
      target = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
    },
    /** What is actually on screen, after smoothing. */
    get voiceBright() { return current; },
    /** What it was last told, before smoothing. */
    get target() { return target; },
    /** Sweep the range on its own. Cancelled by any setVoiceBright call. */
    demo(on = true) { demo = !!on; },
    get demoing() { return demo; },
    /** Bloom strength, for tuning against a real screen. */
    setBloom(strength) { bloom.strength = Number(strength); },
    get bloom() { return bloom.strength; },
    /** Called once per frame with the numbers. Used by ?debug, nothing else. */
    set onFrame(fn) { onFrame = typeof fn === 'function' ? fn : null; },
    get fps() { return fps; },
    stop() { running = false; window.removeEventListener('resize', resize); },
  };
}
