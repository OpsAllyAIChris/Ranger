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
  };

  let socket = null;
  let reply = null; // the card currently being streamed into
  let openToken = null;
  let voiceReady = false;
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

  function entry(item, dismissable) {
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
    return node;
  }

  function section(name, items, { dismissable = false, empty = 'nothing' } = {}) {
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
    for (const item of items) block.append(entry(item, dismissable));
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
      section('Awaiting Confirmation', view.awaiting || [], {
        dismissable: true,
        empty: 'nothing waiting',
      }),
      section('Inbox', view.inbox || [], { dismissable: true, empty: 'nothing new' }),
      section('Drafts', view.drafts || [], { empty: 'none held' }),
      toolSection(view.tools || [])
    );
  }

  // ------------------------------------------------------- the confirmation

  function openCard(event) {
    openToken = event.token;
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
    if (!openToken) return;
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
    });
  }

  async function startListening() {
    if (listening || !voiceReady || openToken) return;
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
        if (!reply) reply = card('Ranger', '');
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
      case 'speech':
        if (speaker) {
          speaker.play(fromBase64(event.audio)).catch((err) => {
            toast('could not play that: ' + err);
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
