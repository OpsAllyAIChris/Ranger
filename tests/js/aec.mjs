/**
 * Run the AEC page's reading against a table of levels and print what it said.
 *
 * The page cannot be opened here -- no sound card, no microphone, no browser --
 * so what is testable is the half that turns numbers into a verdict. That half
 * is the half that says "build it", and a check that only asserted the words
 * `roomBar` and `clearsRoom` appear in the file passed happily with the room
 * term wired to a constant true.
 *
 * Prints one JSON object on stdout. The assertions live in Python.
 */

import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../../ranger/web/aec.html', import.meta.url), 'utf8');
let source = html.slice(html.indexOf('<script type="module">') + 22,
                        html.lastIndexOf('</script>'));
// The wiring at the bottom needs a document; the reading does not.
source = source.replace(/^el\('run.*$/gm, '').replace(/^el\('stop'\)[\s\S]*$/m, '');

function read({ room, off, on, voice, after, settings, noiseSuppression }) {
  const box = {
    className: '', innerHTML: '', textContent: '', hidden: true, style: {},
    appendChild() {}, replaceChildren() {},
    classList: { toggle() {}, remove() {}, add() {} },
  };
  const document = {
    getElementById: () => box,
    createElement: () => box,
    body: { append() {}, remove() {} },
  };
  const judge = new Function('document', source + '; return judge;')(document);
  judge(
    { held: room }, { held: off }, { held: on }, { held: voice }, { held: after },
    settings || { echoCancellation: true },
    !!noiseSuppression,
  );
  return {
    kind: box.className.replace('verdict ', '').trim(),
    text: box.innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(),
  };
}

// The measured run, with a range of fifth passes over the top of it. Room 282,
// echo 1210 uncancelled and 534 cancelled, room again 303: those four are real.
const MEASURED = { room: 282, off: 1210, on: 534, after: 303 };

console.log(JSON.stringify({
  ordinary: read({ ...MEASURED, voice: 3000 }),
  quiet: read({ ...MEASURED, voice: 1200 }),
  veryQuiet: read({ ...MEASURED, voice: 850 }),
  underResidual: read({ ...MEASURED, voice: 500 }),
  // The room term, on its own. The echo is cancelled to nothing and the
  // operator is well over it, but the room is the size of their voice.
  roomTooLoud: read({ room: 2400, off: 9000, on: 534, voice: 3000, after: 2400 }),
  roomChanged: read({ ...MEASURED, after: 4000, voice: 3000 }),
  speakerMuted: read({ room: 282, off: 300, on: 290, voice: 3000, after: 303 }),
  refused: read({ ...MEASURED, voice: 3000, settings: { echoCancellation: false } }),
  withNs: read({ ...MEASURED, voice: 3000, noiseSuppression: true }),
}, null, 1));
