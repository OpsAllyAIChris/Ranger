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
      section('Drafts', view.drafts || [], { clearable: true, empty: 'none held' }),
      toolSection(view.tools || [])
    );
  }

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
