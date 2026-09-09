/**
 * Build the real shell against the fake DOM and report what it rendered.
 *
 * Prints one JSON object on stdout. The assertions live in Python, in
 * tests/test_panel_render.py, so that this file stays a description of what
 * happened rather than an opinion about it.
 */

import { installDom, FakeSocket, Element } from './dom.mjs';

const { document, window, byId } = installDom();
const { createShell } = await import('../../ranger/web/shell.js');

// A stand-in for the scene. The only thing the shell asks of it is where to
// stand, which is the whole of the "the orb is not covered" contract.
const offsets = [];
const assembled = [];
const orb = {
  setOffset(units) { offsets.push(units); },
  assemble(options) { assembled.push(options); return { skip() {}, running: true }; },
};

const shell = createShell(orb);
const socket = FakeSocket.last;

const PANEL = {
  kind: 'panel',
  inbox: [],
  awaiting: [],
  dashlets: [],
  tools: [],
  drafts: [
    { id: 'Ranger/drafts/2026-09-08-telly.md', title: 'Telly follow up',
      detail: 'Dana, pricing lands Thursday.', when: 'today, 09:12', kind: 'md' },
    { id: 'Ranger/drafts/2026-09-08-gp.xlsx', title: 'Gross profit 2026',
      detail: 'Excel workbook, 7 KB', when: 'today, 09:20', kind: 'xlsx' },
  ],
};

const DOCUMENT = {
  kind: 'document',
  format: 'xlsx',
  name: '2026-09-08-gp.xlsx',
  relative: 'Ranger/drafts/2026-09-08-gp.xlsx',
  caveat: 'Sheet values only. Formulas are not shown.',
  blocks: [],
  sheets: [{ name: 'Summary', rows: [['Year', '2026']], total_rows: 1,
             formulas: 0, truncated: false }],
  pages: null,
  total_blocks: 1,
  truncated: false,
  error: '',
  assembly: true,
  particles: 220,
  seconds: 1.1,
  url: '/document/Ranger/drafts/2026-09-08-gp.xlsx',
  download: '/document/Ranger/drafts/2026-09-08-gp.xlsx?download=1',
};

const preview = byId.get('preview') || document.getElementById('preview');
const panel = document.getElementById('panel');

const state = (label) => ({
  label,
  preview: {
    hidden: !!preview.hidden,
    children: preview.children.length,
    tree: preview.describe(),
  },
  bodyClass: document.body.className,
  offsets: [...offsets],
});

const out = { states: [], panel: null, assembled: null, sent: null };

// 1. A fresh session. Nothing has happened yet.
out.states.push(state('load'));

// 2. The panel arrives, with drafts in it.
socket.deliver(PANEL);
out.states.push(state('panel'));
out.panel = panel.describe();

// A click on the clear control of the first draft, to prove it is wired.
const rows = panel.walk().filter((node) => node.classList.contains('entry'));
const clear = rows
  .flatMap((row) => row.walk())
  .find((node) => node.classList.contains('dismiss') && typeof node.onclick === 'function');
if (clear) clear.onclick();
out.sent = socket.sent;

// 3. A document lands.
socket.deliver(DOCUMENT);
out.states.push(state('document'));
out.assembled = assembled;

// 4. Escape closes it.
window.dispatch('keydown', { key: 'Escape' });
out.states.push(state('closed'));

console.log(JSON.stringify(out, null, 1));
