/**
 * Tier 7c. The glass shell.
 *
 * Amendment A holds here as strictly as anywhere: this file decides nothing.
 * It renders frames that arrived over the socket and it sends three kinds of
 * message back. The status dot is set from state frames and never from a local
 * guess, the panel is drawn from what the server read out of the vault, and
 * the confirmation card is a way of collecting a click, not a way of granting
 * anything. The gate is on the other end of the socket and cannot be reached
 * from here except by answering it.
 */

import { createMicrophone, createSpeaker, fromBase64, supported, toBase64 } from './voice.js';

const $ = (id) => document.getElementById(id);

const SOCKET_URL =
  (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';

/** Kept short. The card is the record; this is only so a click feels answered. */
const TOAST_MS = 2600;

/** How long Windows takes to record that a microphone stream has been let go. */
const RELEASE_SETTLE_MS = 700;

export function createShell(orb) {
  const el = {
    status: $('status'),
    cards: $('cards'),
    panel: $('panel'),
    say: $('say'),
    frames: $('frames'),
    toast: $('toast'),
    confirm: $('confirm'),
    togglePanel: $('toggle-panel'),
    toggleFrames: $('toggle-frames'),
    mic: $('mic'),
    micHint: $('mic-hint'),
    liveEdge: $('live-edge'),
    preview: $('preview'),
    handsFree: $('handsfree'),
    handsFreeLabel: $('handsfree-label'),
    banner: $('live-banner'),
    bannerText: $('live-banner-text'),
    windowRing: $('window-ring'),
    bannerStop: $('live-banner-stop'),
  };

  let socket = null;
  let reply = null; // the card currently being streamed into
  let openToken = null;
  let voiceReady = false;
  let handsFree = { offered: false, ready: false, armed: false, phrase: '' };
  let listening = false;
  let microphone = null;
  let speaker = null;

  // ---------------------------------------------------------------- ui bits

  function toast(text) {
    el.toast.textContent = text;
    el.toast.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.toast.classList.remove('show'), TOAST_MS);
  }

  function log(text) {
    el.frames.textContent += text + '\n';
    el.frames.scrollTop = el.frames.scrollHeight;
  }

  function card(who, text, kind) {
    const outer = document.createElement('div');
    outer.className = 'card' + (kind ? ' ' + kind : '');
    const body = document.createElement('div');
    body.className = 'card-body';
    const label = document.createElement('div');
    label.className = 'card-who';
    label.textContent = who;
    const content = document.createElement('div');
    content.className = 'card-text';
    content.textContent = text;
    body.append(label, content);
    outer.append(body);
    el.cards.append(outer);
    while (el.cards.children.length > 6) el.cards.firstChild.remove();
    return content;
  }

  // ------------------------------------------------------------- the panel

  function entry(item, dismissable, clearable) {
    const node = document.createElement('div');
    node.className = 'entry';

    const title = document.createElement('div');
    title.className = 'entry-title';
    title.textContent = item.title;
    node.append(title);

    if (item.detail) {
      const detail = document.createElement('div');
      detail.className = 'entry-detail';
      detail.textContent = item.detail;
      node.append(detail);
    }
    if (item.when) {
      const when = document.createElement('div');
      when.className = 'entry-when';
      when.textContent = item.when;
      node.append(when);
    }
    if (dismissable && item.id) {
      const button = document.createElement('button');
      button.className = 'dismiss clickable';
      button.title = 'dismiss, in the vault as well as here';
      button.textContent = '×';
      button.onclick = () => send({ type: 'dismiss', id: item.id });
      node.append(button);
    }
    // A dropped file. "import" asks the server to propose a column mapping;
    // nothing is parsed until it is clicked, so an accidental drop stays a
    // file in a folder.
    if (item.kind && item.kind.startsWith('import')) {
      node.classList.add('import');
      if (item.kind.endsWith(':table')) {
        const button = document.createElement('button');
        button.className = 'preview-open clickable';
        button.textContent = 'import';
        button.title = 'propose a column mapping. Nothing is written until you confirm';
        button.onclick = () => send({ type: 'import_propose', name: item.title });
        node.append(button);
      }
      return node;
    }
    // A generated document opens its preview. Reopening never replays the
    // assembly animation: it is played once per document, by the server not
    // marking a reopen for it and by seenDocuments here.
    if (item.kind && item.kind !== 'md') {
      node.classList.add('document');
      const open = document.createElement('button');
      open.className = 'preview-open clickable';
      open.textContent = 'preview';
      open.title = 'render this file. The preview is built from the file itself';
      open.onclick = () => send({ type: 'preview', name: item.id });
      node.append(open);
    }
    if (clearable && item.id) {
      // The same tool the model calls. The browser decides nothing here: it
      // sends a name and the server runs clear_draft, so there is one
      // implementation of what clearing means and one place it is logged.
      const button = document.createElement('button');
      button.className = 'dismiss clickable';
      button.title = 'clear from the panel. The draft is moved, never deleted';
      button.textContent = '×';
      button.onclick = () => send({ type: 'clear_draft', name: item.id });
      node.append(button);
    }
    return node;
  }

  function section(name, items, { dismissable = false, clearable = false, empty = 'nothing' } = {}) {
    const block = document.createElement('div');
    block.className = 'section';

    const head = document.createElement('div');
    head.className = 'section-head';
    const label = document.createElement('span');
    label.textContent = name;
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(items.length);
    head.append(label, count);
    block.append(head);

    if (!items.length) {
      const none = document.createElement('div');
      none.className = 'empty';
      none.textContent = empty;
      block.append(none);
      return block;
    }
    for (const item of items) block.append(entry(item, dismissable, clearable));
    return block;
  }

  // A dashlet is a number Python computed, drawn as it arrived. The browser
  // does no arithmetic here on purpose: if the panel formatted or summed
  // anything, there would be two implementations of the figure and no way to
  // tell which one is on screen. See dashlets.py.
  function dashlet(reading) {
    const node = document.createElement('div');
    node.className = 'entry dashlet';

    const title = document.createElement('div');
    title.className = 'entry-title';
    title.textContent = reading.title;
    node.append(title);

    if (reading.error) {
      // Not a value and not an absence: a third state, and it says so. A
      // failed read that rendered blank would read as a legitimate nothing.
      const bad = document.createElement('div');
      bad.className = 'dashlet-error';
      bad.textContent = 'could not read: ' + reading.error;
      node.append(bad);
    } else if (reading.value) {
      const value = document.createElement('div');
      value.className = 'dashlet-value' + (reading.stale ? ' stale' : '');
      value.textContent = reading.value;
      node.append(value);
    } else {
      // ABSENCE IS NEVER ZERO. No value means the words the server sent, never
      // a currency symbol and a nought: a zero looks like a figure that was
      // measured, and it would get acted on.
      const none = document.createElement('div');
      none.className = 'dashlet-empty';
      none.textContent = reading.empty || 'nothing entered';
      node.append(none);
    }

    if (reading.detail) {
      const detail = document.createElement('div');
      detail.className = 'entry-detail';
      detail.textContent = reading.detail;
      node.append(detail);
    }

    // The "as of" line is always drawn when there is anything to date it by.
    // A stale figure that looks current is this dashlet's failure mode, so the
    // age is on screen rather than in a tooltip.
    if (reading.as_of) {
      const when = document.createElement('div');
      when.className = 'entry-when' + (reading.stale ? ' stale' : '');
      const age =
        reading.age_days === null || reading.age_days === undefined
          ? ''
          : reading.age_days === 0
          ? ' (today)'
          : reading.age_days === 1
          ? ' (1 day ago)'
          : ' (' + reading.age_days + ' days ago)';
      when.textContent = 'as of ' + reading.as_of + age + (reading.stale ? ' · stale' : '');
      node.append(when);
    }

    if (reading.key === 'gp') node.append(gpForm(reading));
    return node;
  }

  // The manual entry field. The operator types a figure they were given; the
  // server parses it and writes a create-only note. Nothing is sent to a model
  // and nothing is edited: a correction is a second entry for the same month.
  function gpForm(reading) {
    const form = document.createElement('div');
    form.className = 'dashlet-form';

    const amount = document.createElement('input');
    amount.type = 'text';
    amount.inputMode = 'decimal';
    amount.placeholder = 'GP figure';
    amount.className = 'dashlet-input';
    amount.setAttribute('aria-label', 'gross profit figure');

    const period = document.createElement('input');
    period.type = 'text';
    period.className = 'dashlet-input period';
    period.value = (reading.extra && reading.extra.period) || '';
    period.title = 'the month this figure is for, as 2026-09';
    period.setAttribute('aria-label', 'period, as 2026-09');

    const add = document.createElement('button');
    add.className = 'dashlet-add clickable';
    add.textContent = 'enter';
    add.title = 'record this figure. A second figure for the same month corrects it';

    const submit = () => {
      const value = amount.value.trim();
      if (!value) return;
      send({ type: 'gp_entry', amount: value, period: period.value.trim() });
      amount.value = '';
    };
    add.onclick = submit;
    amount.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    });
    period.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    });

    form.append(amount, period, add);
    return form;
  }

  function dashletSection(items) {
    const block = document.createElement('div');
    block.className = 'section';
    const head = document.createElement('div');
    head.className = 'section-head';
    const label = document.createElement('span');
    label.textContent = 'Numbers';
    head.append(label);
    block.append(head);
    for (const reading of items) block.append(dashlet(reading));
    return block;
  }

  function toolSection(tools) {
    const block = document.createElement('div');
    block.className = 'section';
    const head = document.createElement('div');
    head.className = 'section-head';
    const label = document.createElement('span');
    label.textContent = 'Recent Tools';
    const count = document.createElement('span');
    count.className = 'count';
    count.textContent = String(tools.length);
    head.append(label, count);
    block.append(head);

    if (!tools.length) {
      const none = document.createElement('div');
      none.className = 'empty';
      none.textContent = 'none this session';
      block.append(none);
      return block;
    }
    for (const tool of tools) {
      const node = document.createElement('div');
      node.className = 'entry';
      const row = document.createElement('div');
      row.className = 'tool';
      const mark = document.createElement('span');
      mark.className = 'tool-mark' + (tool.ok ? '' : ' bad');
      mark.textContent = tool.ok ? '✓' : '×';
      const name = document.createElement('span');
      name.className = 'tool-name';
      name.textContent = tool.name;
      row.append(mark, name);
      node.append(row);
      if (tool.summary) {
        const detail = document.createElement('div');
        detail.className = 'entry-detail';
        detail.textContent = tool.summary;
        node.append(detail);
      }
      block.append(node);
    }
    return block;
  }

  function drawPanel(view) {
    el.panel.replaceChildren(
      dashletSection(view.dashlets || []),
      section('Awaiting Confirmation', view.awaiting || [], {
        dismissable: true,
        empty: 'nothing waiting',
      }),
      section('Inbox', view.inbox || [], { dismissable: true, empty: 'nothing new' }),
      section('Drafts', view.drafts || [], { clearable: true, empty: 'none held' }),
      section('Dropped', view.imports || [], { empty: 'drop a file on the window' }),
      toolSection(view.tools || [])
    );
  }

  // --------------------------------------------------------------- the drop
  //
  // A file dropped on the window is POSTed to the server, which lands it in
  // today's import folder and says what it got. **Nothing is parsed here and
  // nothing is parsed there.** The browser does not read the file, does not
  // name it anything of its own, and does not decide what it is: it hands over
  // the bytes and the name the operator's own file system gave it.
  //
  // A POST rather than a socket message because the socket caps a message at a
  // megabyte, deliberately, and a spreadsheet is not a sentence.

  let dropDepth = 0;

  function dropping(on) {
    dropDepth = on ? dropDepth + 1 : Math.max(0, dropDepth - 1);
    document.body.classList.toggle('dropping', dropDepth > 0);
  }

  // The server refuses an oversized drop on the Content-Length, without
  // reading it. That is the guard; this is so the operator gets a sentence
  // rather than a failed upload, and so a 400MB file is not pushed through a
  // socket to be told no at the other end.
  let dropCeiling = 25 * 1024 * 1024;

  async function sendFile(file) {
    if (file.size > dropCeiling) {
      toast(
        file.name + ' is ' + (file.size / 1048576).toFixed(1) +
        ' MB, over the ' + (dropCeiling / 1048576).toFixed(0) + ' MB limit for a drop'
      );
      return;
    }
    toast('taking ' + file.name + '...');
    try {
      const response = await fetch('/drop', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Ranger-Filename': encodeURIComponent(file.name),
        },
        body: file,
      });
      const result = await response.json();
      toast(result.message || (result.ok ? 'landed' : 'refused'));
      if (!result.ok) return;
      // The server looks at what landed: if the headers are a shape the
      // operator has already mapped it imports, and if they are not it says so
      // and waits. Either way the panel is redrawn from the vault.
      send({ type: 'dropped', name: result.name });
    } catch (err) {
      toast('that drop did not reach Jarvis: ' + err);
    }
  }

  window.addEventListener('dragenter', (e) => { e.preventDefault(); dropping(true); });
  window.addEventListener('dragover', (e) => { e.preventDefault(); });
  window.addEventListener('dragleave', (e) => { e.preventDefault(); dropping(false); });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dropDepth = 0;
    document.body.classList.remove('dropping');
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    for (const file of files) sendFile(file);
  });

  // ------------------------------------------------------------ the preview
  //
  // Where it lives, and why here: a sheet on the left, over the starfield,
  // never over the orb. The orb is the one thing that says what state Jarvis
  // is in, so it steps aside (orb.setOffset) rather than being covered. The
  // activity panel keeps its place on the right; a document does not belong in
  // a 260px column.
  //
  // Everything drawn here came out of the file on disk. The server parsed the
  // .docx and .xlsx with the standard library and sent the parts; the PDF is
  // the browser's own viewer pointed at the real bytes. Nothing here renders
  // what the model said it was going to write.

  const seenDocuments = new Set();
  const reducedMotion =
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let assembling = null;
  let previewOpen = null;

  function endAssembly() {
    if (assembling) {
      assembling.skip();
      assembling = null;
    }
  }

  function closePreview() {
    endAssembly();
    previewOpen = null;
    el.preview.hidden = true;
    el.preview.replaceChildren();
    document.body.classList.remove('previewing');
    if (orb && orb.setOffset) orb.setOffset(0);
  }

  // `event.kind` is the frame type ('document') for every message on this
  // socket, so a document's own format arrives as `event.format`.
  function previewChrome(event) {
    const head = document.createElement('div');
    head.className = 'preview-head';

    const name = document.createElement('div');
    name.className = 'preview-name';
    const badge = document.createElement('span');
    badge.className = 'preview-badge ' + event.format;
    badge.textContent = event.format;
    const label = document.createElement('span');
    label.textContent = event.name;
    name.append(badge, label);

    const actions = document.createElement('div');
    actions.className = 'preview-actions';

    // Both ways out of the window, because they are different acts. Reveal
    // shows the operator where the file is so they can drag it into an email;
    // download hands them a copy through the browser. Neither opens Word.
    const reveal = document.createElement('button');
    reveal.className = 'ghost clickable';
    reveal.textContent = 'show in folder';
    reveal.title = 'open the folder with this file selected. Does not open Word';
    reveal.onclick = () => send({ type: 'reveal', name: event.relative });

    const download = document.createElement('a');
    download.className = 'ghost clickable';
    download.textContent = 'download';
    download.href = event.download;
    download.setAttribute('download', event.name);

    const close = document.createElement('button');
    close.className = 'ghost clickable';
    close.textContent = 'close';
    close.onclick = closePreview;

    actions.append(reveal, download, close);
    head.append(name, actions);

    // The caveat is chrome, not a footnote. A .docx preview that did not say
    // it was an approximation would be a claim about Word that this cannot
    // make.
    const caveat = document.createElement('div');
    caveat.className = 'preview-caveat';
    caveat.textContent = event.caveat;

    return [head, caveat];
  }

  function previewBody(event) {
    const body = document.createElement('div');
    body.className = 'preview-body ' + event.format;

    if (event.error) {
      const bad = document.createElement('div');
      bad.className = 'preview-error';
      bad.textContent = 'could not read this file: ' + event.error;
      body.append(bad);
      return body;
    }

    if (event.format === 'pdf') {
      // The real PDF, in the browser's own viewer. Exact, and it scrolls every
      // page: a forty page document is forty pages here.
      const frame = document.createElement('iframe');
      frame.className = 'preview-pdf';
      frame.src = event.url;
      frame.title = event.name;
      body.append(frame);
      return body;
    }

    if (event.format === 'xlsx') {
      for (const sheet of event.sheets || []) {
        const block = document.createElement('div');
        block.className = 'preview-sheet';
        const title = document.createElement('div');
        title.className = 'preview-sheet-name';
        title.textContent = sheet.name;
        block.append(title);
        if (sheet.formulas) {
          const note = document.createElement('div');
          note.className = 'preview-note';
          note.textContent =
            sheet.formulas + ' cell(s) hold a formula. Formulas are not shown here.';
          block.append(note);
        }
        block.append(grid(sheet.rows, true));
        if (sheet.truncated) block.append(more(sheet.rows.length, sheet.total_rows, 'rows'));
        body.append(block);
      }
      return body;
    }

    for (const item of event.blocks || []) {
      if (item.kind === 'table') {
        body.append(grid(item.rows || [], true));
      } else if (item.kind === 'heading') {
        const node = document.createElement('div');
        node.className = 'preview-h preview-h' + Math.min(4, item.level || 1);
        node.textContent = item.text;
        body.append(node);
      } else if (item.kind === 'bullet') {
        const node = document.createElement('div');
        node.className = 'preview-bullet';
        node.textContent = item.text;
        body.append(node);
      } else {
        const node = document.createElement('div');
        node.className = 'preview-p';
        node.textContent = item.text;
        body.append(node);
      }
    }
    if (event.truncated) {
      body.append(more((event.blocks || []).length, event.total_blocks, 'blocks'));
    }
    return body;
  }

  function grid(rows, header) {
    const wrap = document.createElement('div');
    wrap.className = 'preview-grid-wrap';
    const table = document.createElement('table');
    table.className = 'preview-grid';
    rows.forEach((row, index) => {
      const tr = document.createElement('tr');
      for (const cell of row) {
        const td = document.createElement(header && index === 0 ? 'th' : 'td');
        td.textContent = cell;
        tr.append(td);
      }
      table.append(tr);
    });
    wrap.append(table);
    return wrap;
  }

  function more(shown, total, what) {
    const node = document.createElement('div');
    node.className = 'preview-note';
    node.textContent =
      'showing ' + shown + ' of ' + total + ' ' + what +
      '. Open the file itself for the rest.';
    return node;
  }

  // The mapping card. It lives in the preview sheet because the sheet is
  // already where file-derived content is shown, and because a proposal has to
  // show what it will write: a confirmation that does not show the rows is a
  // click, not a decision.
  function openProposal(event) {
    endAssembly();
    previewOpen = event.relative;

    const head = document.createElement('div');
    head.className = 'preview-head';
    const name = document.createElement('div');
    name.className = 'preview-name';
    const badge = document.createElement('span');
    badge.className = 'preview-badge import';
    badge.textContent = 'import';
    const label = document.createElement('span');
    label.textContent = event.name;
    name.append(badge, label);
    const close = document.createElement('button');
    close.className = 'ghost clickable';
    close.textContent = 'close';
    close.onclick = closePreview;
    const actions = document.createElement('div');
    actions.className = 'preview-actions';
    actions.append(close);
    head.append(name, actions);

    const caveat = document.createElement('div');
    caveat.className = 'preview-caveat';
    // Where Jarvis will not guess, first: two columns that could both be the
    // figure is a question, and the card says so rather than preselecting one.
    caveat.textContent = event.ambiguity ? event.ambiguity : event.known
      ? 'Jarvis has seen this shape before. Confirm to import it again.'
      : 'Jarvis has not seen this shape before. It has proposed a mapping from the '
        + 'column headings; nothing is written until you confirm it.';

    const body = document.createElement('div');
    body.className = 'preview-body import';

    const form = document.createElement('div');
    form.className = 'import-form';
    const pick = (which, selected) => {
      const wrap = document.createElement('label');
      wrap.className = 'import-pick';
      const text = document.createElement('span');
      text.textContent = which === 'period' ? 'period column' : 'gross profit column';
      const select = document.createElement('select');
      select.className = 'import-select';
      for (const header of event.headers || []) {
        const option = document.createElement('option');
        option.value = header;
        option.textContent = header;
        if (header === selected) option.setAttribute('selected', 'selected');
        select.append(option);
      }
      select.value = selected || '';
      wrap.append(text, select);
      form.append(wrap);
      return select;
    };
    const period = pick('period', event.period);
    const amount = pick('amount', event.amount);
    body.append(form);

    if (event.refused) {
      const bad = document.createElement('div');
      bad.className = 'preview-error';
      bad.textContent = event.refused;
      body.append(bad);
    }

    const summary = document.createElement('div');
    summary.className = 'preview-note';
    summary.textContent = event.summary || 'nothing to write from this mapping yet';
    body.append(summary);

    // Exactly what it will write, before it writes it.
    const rows = (event.changes || []).map((change) => [
      change.period,
      change.amount,
      change.verdict === 'corrects' ? 'was ' + change.was : change.verdict,
    ]);
    if (rows.length) body.append(grid([['period', 'figure', ''], ...rows], true));
    if ((event.skipped || []).length) {
      const skipped = document.createElement('div');
      skipped.className = 'preview-note';
      skipped.textContent =
        'not read as months: ' + event.skipped.slice(0, 8).join(', ');
      body.append(skipped);
    }

    const confirm = document.createElement('button');
    confirm.className = 'import-confirm clickable';
    confirm.textContent = 'import these figures';
    confirm.onclick = () => {
      send({
        type: 'import_apply',
        name: event.name,
        period: period.value,
        amount: amount.value,
      });
      closePreview();
    };
    body.append(confirm);

    el.preview.replaceChildren(head, caveat, body);
    el.preview.hidden = false;
    document.body.classList.add('previewing');
    if (orb && orb.setOffset) orb.setOffset(4.2);
  }

  // An analysis, in the same sheet. Not a second panel: same open and close,
  // same orb offset, same escape order, same close control. What is drawn is a
  // file Python wrote under Ranger/analysis before anything reached the
  // browser, so "export this" is a format change of the thing on screen.
  function openAnalysis(event) {
    endAssembly();
    previewOpen = event.relative;

    const head = document.createElement('div');
    head.className = 'preview-head';
    const name = document.createElement('div');
    name.className = 'preview-name';
    const badge = document.createElement('span');
    badge.className = 'preview-badge analysis';
    badge.textContent = 'computed';
    const label = document.createElement('span');
    label.textContent = event.title || 'Analysis';
    name.append(badge, label);

    const actions = document.createElement('div');
    actions.className = 'preview-actions';
    const excel = document.createElement('button');
    excel.className = 'ghost clickable';
    excel.textContent = 'export xlsx';
    excel.title = 'the same table, as a workbook in your drafts folder';
    excel.onclick = () =>
      send({ type: 'analysis_export', relative: event.relative, format: 'xlsx' });
    const copy = document.createElement('button');
    copy.className = 'ghost clickable';
    copy.textContent = 'copy';
    copy.title = 'copy the table as text';
    copy.onclick = () => {
      const text = [event.columns.join('\t')]
        .concat((event.rows || []).map((row) => row.join('\t')))
        .join('\n');
      if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
      toast('copied ' + (event.rows || []).length + ' rows');
    };
    const close = document.createElement('button');
    close.className = 'ghost clickable';
    close.textContent = 'close';
    close.onclick = closePreview;
    actions.append(excel, copy, close);
    head.append(name, actions);

    // Which file and which sheet, always on screen. A table with no source on
    // it is a number nobody can check afterwards.
    const caveat = document.createElement('div');
    caveat.className = 'preview-caveat';
    caveat.textContent =
      'Computed in Python from ' + (event.source || 'a dropped file') +
      (event.sheet ? ' (' + event.sheet + ')' : '') + '. Nothing here was written by a model.';

    const body = document.createElement('div');
    body.className = 'preview-body analysis';

    if (event.summary) {
      const summary = document.createElement('div');
      summary.className = 'preview-p';
      summary.textContent = event.summary;
      body.append(summary);
    }

    // Headers as they were in the source, never renamed: a column renamed on
    // the way to the screen is a column that cannot be found again in the
    // operator's own export.
    body.append(grid([event.columns || []].concat(event.rows || []), true));

    if (event.truncated) {
      body.append(more((event.rows || []).length, event.total_rows, 'rows'));
    }
    if ((event.provenance || []).length) {
      const where = document.createElement('div');
      where.className = 'preview-h preview-h3';
      where.textContent = 'Where this came from';
      body.append(where);
      for (const line of event.provenance) {
        const item = document.createElement('div');
        item.className = 'preview-bullet';
        item.textContent = line;
        body.append(item);
      }
    }

    el.preview.replaceChildren(head, caveat, body);
    el.preview.hidden = false;
    document.body.classList.add('previewing');
    if (orb && orb.setOffset) orb.setOffset(4.2);
  }

  function openPreview(event) {
    // The content is drawn first and the flourish plays over it. The animation
    // never gates the document: if the particles are switched off, or the
    // machine prefers reduced motion, this is exactly the same screen.
    endAssembly();
    previewOpen = event.relative;
    el.preview.replaceChildren(...previewChrome(event), previewBody(event));
    el.preview.hidden = false;
    document.body.classList.add('previewing');
    if (orb && orb.setOffset) orb.setOffset(4.2);

    const first = !seenDocuments.has(event.relative);
    seenDocuments.add(event.relative);
    if (event.assembly && first && !reducedMotion && orb && orb.assemble) {
      const rect = el.preview.getBoundingClientRect();
      assembling = orb.assemble({
        count: event.particles,
        seconds: event.seconds,
        rect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
      });
    }
  }

  // Click to skip, anywhere, on the first event of any kind. The particles are
  // a flourish and a flourish that has to be waited out is a tax.
  window.addEventListener('pointerdown', endAssembly, { capture: true });
  window.addEventListener('wheel', endAssembly, { capture: true, passive: true });

  // ------------------------------------------------------- the confirmation

  function openCard(event) {
    openToken = event.token;
    // A spoken yes is not consent, and hands free is a microphone that is
    // already open. It goes off, not just quiet.
    if (handsFree.armed) killHandsFree();
    // A spoken yes is not consent, so while a card is open there is no
    // microphone to say it into. Tier 3's rule, enforced by the interface
    // rather than by hoping the operator does not try.
    if (listening) stopListening(true);
    el.mic.disabled = true;
    el.confirm.hidden = false;
    el.confirm.replaceChildren(buildCard(event));
    el.confirm.querySelector('.decline').focus();
  }

  function buildCard(event) {
    const card = document.createElement('div');
    card.className = 'confirm-card';

    const head = document.createElement('div');
    head.className = 'confirm-head';
    head.textContent = 'Needs your yes';

    const action = document.createElement('div');
    action.className = 'confirm-action';
    // The words the gate itself used. Not a summary of them, and not a
    // friendlier version: the operator judges the thing that will actually run.
    action.textContent = event.action;

    const meta = document.createElement('div');
    meta.className = 'confirm-meta';
    meta.textContent = `${event.tool}  ${event.origin}  ${event.token}`;

    const buttons = document.createElement('div');
    buttons.className = 'confirm-buttons';
    const decline = document.createElement('button');
    decline.className = 'decline clickable';
    decline.textContent = 'No, do not';
    decline.onclick = () => answer(false);
    const approve = document.createElement('button');
    approve.className = 'approve clickable';
    approve.textContent = 'Yes, once';
    approve.onclick = () => answer(true);
    buttons.append(decline, approve);

    card.append(head, action, meta, buttons);
    return card;
  }

  function answer(allow) {
    if (!openToken) return;
    send({ type: 'decision', token: openToken, allow });
    closeCard();
  }

  function closeCard() {
    openToken = null;
    el.mic.disabled = false;
    el.confirm.hidden = true;
    el.confirm.replaceChildren();
  }

  // Escape declines. There is no way to dismiss this card without answering
  // it, and the answer a stray keypress gives is the safe one.
  window.addEventListener('keydown', (e) => {
    // Escape kills hands free before anything else looks at it. Condition
    // three, and the one key that must never be ambiguous.
    if (e.key === 'Escape' && handsFree.armed) {
      e.preventDefault();
      killHandsFree();
      toast('hands free off');
      return;
    }
    if (!openToken) {
      // Third in the order, so escape never takes the preview instead of the
      // microphone or a card that is waiting for an answer.
      if (e.key === 'Escape' && previewOpen) { e.preventDefault(); closePreview(); }
      if (e.key === 'Escape') endAssembly();
      return;
    }
    if (e.key === 'Escape') { e.preventDefault(); answer(false); }
    // Enter is deliberately not bound. Approving is a decision, not a reflex.
  });

  // -------------------------------------------------------------- the mic

  function micState(state) {
    // data-state is the only thing set here. The stylesheet decides which
    // icon that means, because setting .hidden on an svg does nothing.
    el.mic.dataset.state = state;
    const live = state === 'live';
    el.micHint.classList.toggle('live', live);
    el.liveEdge.classList.toggle('on', live);
    el.micHint.textContent = live ? 'listening, click to send' : 'hold space to talk';
    el.mic.title = live ? 'click to stop and send' : 'click to talk, or hold space';
  }

  function ensureAudio() {
    if (microphone) return;
    microphone = createMicrophone({
      // While recording, the operator's own voice drives the orb. It is the
      // clearest possible signal that the microphone is actually open.
      onLevel: (level) => { if (listening && orb) orb.setVoiceBright(level); },
      onError: (err) => { toast('microphone error: ' + err); stopListening(true); },
    });
    speaker = createSpeaker({
      onLevel: (level) => { if (!listening && orb) orb.setVoiceBright(level); },
      // Half of the conversation window's anchor. The server has the other
      // half, the turn completing, and opens the window only when the chunk
      // played is the last chunk sent. Reporting the index rather than the
      // bare event is what stops a reply that drains three times opening
      // three windows.
      onDone: (index) => send({ type: 'spoken', index }),
    });
  }

  async function startListening() {
    if (listening || !voiceReady || openToken) return;
    if (handsFree.armed) {
      // Python has the microphone while armed. Two consumers would both work
      // on Windows and neither would be what the operator meant.
      toast('hands free is on. Turn it off to hold the button instead');
      return;
    }
    ensureAudio();
    // Unlocked from inside the click that started this, or the browser will
    // refuse to play the reply and nothing will say why.
    speaker.unlock().catch(() => {});
    if (speaker.speaking) speaker.stop();   // talking over it means stop it

    try {
      await microphone.start();
    } catch (err) {
      toast('no microphone: ' + (err && err.message ? err.message : err));
      return;
    }
    listening = true;
    micState('live');
  }

  async function stopListening(discard) {
    if (!listening) return;
    listening = false;
    micState('idle');
    if (orb) orb.setVoiceBright(0);

    let recording = null;
    try {
      recording = await microphone.stop();
    } catch (err) {
      toast('could not finish the recording: ' + err);
      return;
    }
    if (discard || !recording) {
      if (!discard) toast('that was too short to send');
      return;
    }

    let audio;
    try {
      audio = await toBase64(recording.blob);
    } catch (err) {
      toast('could not read the recording: ' + err);
      return;
    }
    card('You', '\u2026', 'you');
    send({
      type: 'audio',
      audio,
      mime: recording.mime,
      seconds: recording.seconds,
      interrupt: true,
    });
  }

  function toggleMic() {
    if (listening) stopListening(false);
    else startListening();
  }

  // Hold space to talk, anywhere except while typing in the field. Released
  // early enough to be a slip is discarded rather than sent.
  let heldSince = 0;
  window.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.repeat) return;
    if (document.activeElement === el.say || openToken) return;
    e.preventDefault();
    heldSince = performance.now();
    startListening();
  });
  window.addEventListener('keyup', (e) => {
    if (e.code !== 'Space') return;
    if (document.activeElement === el.say) return;
    if (!listening) return;
    e.preventDefault();
    stopListening(performance.now() - heldSince < 200);
  });

  el.mic.onclick = () => {
    toggleMic();
    // The seam 7d was specced to expose. Nothing in this file listens to it;
    // it is there so anything else can.
    window.dispatchEvent(new CustomEvent('ranger:mic-toggle', { detail: { listening } }));
  };

  // -------------------------------------------------------- hands free

  function drawHandsFree(state) {
    handsFree = Object.assign({}, handsFree, state || {});
    el.handsFree.classList.toggle('on', Boolean(handsFree.offered));
    const armed = Boolean(handsFree.armed);

    el.handsFree.querySelector('.pip').style.visibility = armed ? 'visible' : 'hidden';
    el.handsFreeLabel.textContent = armed ? 'listening' : 'hands free';
    el.handsFree.setAttribute('aria-pressed', String(armed));

    // The edge is shared with recording, so the class decides which it means.
    el.liveEdge.classList.toggle('hands-free', armed);
    el.liveEdge.classList.toggle('on', armed || listening);
    el.banner.classList.toggle('on', armed);
    document.body.classList.toggle('hands-free', armed);
    if (armed) {
      el.bannerText.textContent = 'microphone open, listening for "' + handsFree.phrase + '"';
    }
  }

  /** The conversation window: still listening, and for how much longer.
   *
   * The banner already says the microphone is open. This says the second
   * thing, which is that this particular opening was not asked for by name and
   * will close by itself. Restarting the animation means removing the class
   * and forcing a reflow; without that a second window reuses the finished
   * animation and the ring sits empty while the microphone is live.
   */
  function drawWindow(state) {
    const ring = el.windowRing;
    if (!ring) return;
    if (!state.open) {
      ring.hidden = true;
      ring.classList.remove('draining');
      if (handsFree.armed) {
        el.bannerText.textContent =
          'microphone open, listening for "' + handsFree.phrase + '"';
      }
      return;
    }
    ring.hidden = false;
    ring.classList.remove('draining');
    void ring.getBoundingClientRect();
    ring.style.setProperty('--window-seconds', state.seconds + 's');
    ring.classList.add('draining');
    el.bannerText.textContent =
      'still listening, no need to say the phrase (' + state.used + ' of ' + state.of + ')';
  }

  async function armHandsFree() {
    if (!handsFree.offered) return;
    if (!handsFree.ready) {
      toast(handsFree.reason || 'hands free is not installed');
      return;
    }
    // Unlocked from inside this click, as with the mic button, or the reply
    // to a hands free turn cannot be played.
    ensureAudio();
    speaker.unlock().catch(() => {});

    // Put the browser's microphone down before asking Python for it. Windows
    // records microphone use per application and cannot tell one Chrome from
    // another, so a stream this page is still holding reads as "somebody else
    // has the microphone" and hands free refuses to arm for itself.
    if (listening) await stopListening(true);
    microphone.release();
    el.micHint.textContent = 'starting hands free';

    // Windows writes the release promptly, but not instantly. Waiting is
    // honest; retrying until it passes would be a way of not taking no for an
    // answer, and no is the answer that matters here.
    await new Promise((resolve) => setTimeout(resolve, RELEASE_SETTLE_MS));
    send({ type: 'arm' });
  }

  function killHandsFree() {
    if (!handsFree.armed) return false;
    send({ type: 'disarm' });
    // Drawn immediately rather than waiting for the round trip. Condition
    // three is one click to kill, and a kill that looks like it might not
    // have worked is not one.
    drawHandsFree({ armed: false });
    return true;
  }

  document.addEventListener('visibilitychange', () => {
    send({ type: 'visible', visible: document.visibilityState === 'visible' });
  });

  el.handsFree.onclick = () => {
    if (handsFree.armed) killHandsFree();
    else armHandsFree();
  };
  el.bannerStop.onclick = killHandsFree;

  // ------------------------------------------------------------ the socket

  function send(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      toast('not connected');
      return;
    }
    socket.send(JSON.stringify(payload));
  }

  function setState(state) {
    el.status.dataset.state = state;
    el.status.title = state.replace(/_/g, ' ');
    // The orb is driven by real amplitude now, from the microphone while
    // listening and from the loudspeaker while speaking. State only puts it
    // back to rest, and only when neither is running.
    if (orb && !listening && !(speaker && speaker.speaking) && state !== 'speaking') {
      orb.setVoiceBright(0);
    }
  }

  function onEvent(event) {
    switch (event.kind) {
      case 'hello':
        setState('idle');
        if (event.drop_max_bytes) dropCeiling = event.drop_max_bytes;
        drawHandsFree(event.hands_free);
        voiceReady = Boolean(event.voice) && supported();
        el.mic.hidden = !voiceReady;
        if (event.voice && !supported()) {
          toast('this browser cannot record audio, so typing is the way in');
        } else if (!event.voice) {
          // Said once, quietly. The interface still works without a key.
          log('-- no transcription configured, so there is no microphone');
        }
        break;
      case 'state':
        setState(event.state);
        break;
      case 'text':
        if (!reply) reply = card('Jarvis', '');
        reply.textContent += event.text;
        break;
      case 'heard': {
        // Replace the placeholder card with what was actually understood, so
        // a mishearing is visible at the moment it happens rather than being
        // inferred from a strange answer.
        const cards = el.cards.querySelectorAll('.card.you .card-text');
        const last = cards[cards.length - 1];
        if (last && last.textContent === '\u2026') {
          last.textContent = event.text || '(nothing heard)';
        }
        if (event.shaky && event.shaky.length) {
          toast('unsure about: ' + event.shaky.join(', '));
        }
        break;
      }
      case 'hands_free':
        drawHandsFree(event);
        if (!event.armed) el.micHint.textContent = 'hold space to talk';
        break;
      case 'dismissed_aloud':
        // The minimise itself happens in Python, through the window handle.
        // window.blur() was tried here and is ignored in Chrome's app mode, so
        // the browser now only reports what happened rather than attempting it.
        // The microphone stays on: a spoken phrase never changes the safety
        // state, only what is on screen.
        log('-- dismissed (' + (event.outcome || 'unknown') + '): ' + event.text);
        break;
      case 'window':
        drawWindow(event);
        break;
      case 'wake_fire':
        // Every firing, including the discarded ones. It is in the audit log
        // either way; this is so the operator sees it happen.
        log('-- wake: ' + event.detail);
        if (event.outcome !== 'spoke') toast(event.detail);
        break;
      case 'level':
        // While Python owns the microphone the input level arrives over the
        // socket rather than being measured here. Playback is unchanged.
        if (orb && handsFree.armed && !listening) orb.setVoiceBright(event.value);
        break;
      case 'speech':
        if (speaker) {
          speaker.play(fromBase64(event.audio), event.format, event.index).catch((err) => {
            toast('could not play that: ' + err);
            console.error('playback failed', event.format, err);
          });
        }
        break;
      case 'stopped':
        if (speaker) speaker.stop();
        reply = null;
        el.say.disabled = false;
        break;
      case 'panel':
        drawPanel(event);
        break;
      case 'analysis':
        openAnalysis(event);
        break;
      case 'import_proposal':
        openProposal(event);
        break;
      case 'document':
        // Sent only for a file that is on disk. The animation starts here and
        // nowhere else, so it cannot begin over a generation that failed.
        openPreview(event);
        break;
      case 'confirm_open':
        openCard(event);
        break;
      case 'confirm_closed':
        if (openToken === event.token) closeCard();
        break;
      case 'dismissed':
        toast('dismissed in the vault: ' + event.title);
        break;
      case 'notice':
        toast(event.message);
        break;
      case 'error':
        toast(event.message);
        break;
      case 'done':
        reply = null;
        el.say.disabled = false;
        el.say.focus();
        break;
      default:
        break;
    }
  }

  function connect() {
    socket = new WebSocket(SOCKET_URL);
    socket.onopen = () => toast('connected');
    socket.onclose = (e) => {
      setState('idle');
      if (listening) stopListening(true);
      if (speaker) speaker.stop();
      drawHandsFree({ armed: false, offered: handsFree.offered });
      closeCard();
      el.say.disabled = true;
      toast('socket closed (' + e.code + '). reload to reconnect.');
    };
    socket.onerror = () => toast('socket error. is "ranger ui" still running?');
    socket.onmessage = (e) => {
      log(e.data);
      let event;
      try {
        event = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      onEvent(event);
    };
  }

  // ------------------------------------------------------------- the wiring

  el.say.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    const text = el.say.value.trim();
    if (!text) return;
    card('You', text, 'you');
    send({ type: 'turn', text });
    el.say.value = '';
    el.say.disabled = true;
  });

  el.togglePanel.onclick = () => {
    const collapsed = el.panel.classList.toggle('collapsed');
    el.togglePanel.setAttribute('aria-pressed', String(!collapsed));
  };
  el.toggleFrames.onclick = () => {
    el.frames.hidden = !el.frames.hidden;
    el.toggleFrames.setAttribute('aria-pressed', String(!el.frames.hidden));
  };

  connect();
  return {
    send,
    connect,
    toggleMic,
    get listening() { return listening; },
  };
}
