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
  imports: [
    { id: 'Ranger/imports/2026-09-09/netsuite gp.xlsx', title: 'netsuite gp.xlsx',
      detail: 'Excel workbook, 6 KB, not read yet', when: '2026-09-09',
      kind: 'import:table' },
    { id: 'Ranger/imports/2026-09-09/pricing.pdf', title: 'pricing.pdf',
      detail: 'PDF, 2 KB', when: '2026-09-09', kind: 'import' },
  ],
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

// A click on the import button of the dropped spreadsheet.
const importButton = panel
  .walk()
  .find((node) => node.classList.contains('preview-open') && node.textContent === 'import');
if (importButton) importButton.onclick();

// 3. A document lands.
socket.deliver(DOCUMENT);
out.states.push(state('document'));
out.assembled = assembled;

// 4. Escape closes it.
window.dispatch('keydown', { key: 'Escape' });
out.states.push(state('closed'));

// 5. An analysis, in the same sheet.
socket.deliver({
  kind: 'analysis',
  title: 'Commission by account, August against July',
  summary: '3 Account(s), total of Commission, totalling 3,530.75.',
  columns: ['Account', 'Commission', 'Commission (Jul 2026)', 'Change', 'Rows'],
  rows: [
    ['Illes Foods', '1,240.50', '1,100.00', '140.50', '1'],
    ['Rusty Supply', '980.00', '1,010.00', '-30.00', '1'],
  ],
  provenance: ['file: commission.xls', 'sheet: Table 1', 'rows read: 8, groups out: 3'],
  total_rows: 3,
  truncated: true,
  source: 'commission.xls',
  sheet: 'Table 1',
  relative: 'Ranger/analysis/2026-09-09/commission 100000.md',
});
out.states.push(state('analysis'));
out.analysis = preview.describe();

const exporter = preview
  .walk()
  .find((node) => node.textContent === 'export xlsx' && typeof node.onclick === 'function');
if (exporter) exporter.onclick();
out.sentAfterExport = [...socket.sent];

window.dispatch('keydown', { key: 'Escape' });
out.states.push(state('analysis closed'));

// 6. A mapping proposal, which is the other thing the sheet is used for.
socket.deliver({
  kind: 'import_proposal',
  name: 'netsuite gp.xlsx',
  relative: 'Ranger/imports/2026-09-09/netsuite gp.xlsx',
  known: false,
  headers: ['Period', 'Revenue', 'COGS', 'Gross Profit', 'Margin %'],
  period: 'Period',
  amount: 'Gross Profit',
  fingerprint: '7f816472',
  changes: [
    { period: '2026-06', amount: '41000', was: null, verdict: 'new' },
    { period: '2026-08', amount: '47900', was: '48250', verdict: 'corrects' },
  ],
  skipped: ['Total 573500'],
  refused: '',
  summary: '2 months updated, 0 unchanged, 1 row skipped',
});
out.states.push(state('proposal'));
out.proposal = preview.describe();

// Confirming it is the only way a mapping is ever learned.
const confirm = preview
  .walk()
  .find((node) => node.classList.contains('import-confirm'));
if (confirm) confirm.onclick();
out.sentAfterConfirm = socket.sent;
out.states.push(state('after confirm'));

console.log(JSON.stringify(out, null, 1));
