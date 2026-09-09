/**
 * The smallest DOM that shell.js will run against.
 *
 * There was no way to assert on what the interface actually renders. Two bugs
 * shipped in one commit because of it: a preview sheet that was `hidden` and
 * on screen anyway, and a clear button that renders and is invisible. Both are
 * about the rendered result, and every test we had looked at Python.
 *
 * This is not a browser and does not pretend to be one. It is enough of the
 * DOM for `createShell` to build its tree, plus a record of what was built, so
 * a test can ask "is there a clear button on that row" and get a real answer.
 * Anything it cannot answer -- layout, paint, CSS cascade -- is asserted
 * against the stylesheet separately, in Python.
 */

class ClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...names) { for (const n of names) this.set.add(n); }
  remove(...names) { for (const n of names) this.set.delete(n); }
  contains(name) { return this.set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.set.has(name) : !!force;
    if (on) this.set.add(name); else this.set.delete(name);
    return !on ? false : true;
  }
  toString() { return [...this.set].join(' '); }
}

export class Element {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parent = null;
    this.attributes = {};
    this.style = {};
    this.dataset = {};
    this.listeners = {};
    this._class = new ClassList(this);
    this.hidden = false;
    this.disabled = false;
    this.textContent = '';
    this.value = '';
  }
  get classList() { return this._class; }
  get className() { return this._class.toString(); }
  set className(value) {
    this._class.set = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
  get firstChild() { return this.children[0] || null; }
  append(...nodes) {
    for (const node of nodes) {
      if (node === undefined || node === null) continue;
      node.parent = this;
      this.children.push(node);
    }
  }
  appendChild(node) { this.append(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  hasAttribute(name) { return name in this.attributes; }
  addEventListener(kind, fn) { (this.listeners[kind] ||= []).push(fn); }
  dispatch(kind, event = {}) {
    for (const fn of this.listeners[kind] || []) fn({ preventDefault() {}, ...event });
  }
  focus() {}
  getBoundingClientRect() { return { left: 0, top: 56, width: 640, height: 700 }; }
  /** Every node under this one, this one included. */
  walk() {
    return [this, ...this.children.flatMap((child) => child.walk())];
  }
  querySelector(selector) {
    const wanted = selector.replace('.', '');
    return this.walk().find((node) =>
      selector.startsWith('.')
        ? node.classList.contains(wanted)
        : node.tagName === selector.toUpperCase()
    ) || null;
  }
  /** What a test reads: the shape of what was rendered. */
  describe() {
    return {
      tag: this.tagName.toLowerCase(),
      class: this.className,
      text: this.textContent,
      title: this.attributes.title || this.title || '',
      hidden: !!this.hidden,
      clickable: typeof this.onclick === 'function',
      children: this.children.map((child) => child.describe()),
    };
  }
}

export class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    FakeSocket.last = this;
  }
  send(text) { this.sent.push(JSON.parse(text)); }
  close() {}
  /** Deliver a frame exactly as the server would. */
  deliver(event) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(event) });
  }
}

export function installDom() {
  const byId = new Map();
  const document = {
    createElement: (tag) => new Element(tag),
    getElementById(id) {
      if (!byId.has(id)) {
        const node = new Element(id === 'frames' ? 'pre' : 'div');
        node.id = id;
        // The interface's own markup starts with these hidden, and whether
        // they stay that way is exactly what a test needs to see.
        if (['preview', 'confirm', 'frames'].includes(id)) node.hidden = true;
        byId.set(id, node);
      }
      return byId.get(id);
    },
    body: new Element('body'),
    addEventListener() {},
  };
  const window = {
    addEventListener(kind, fn) { (window.listeners[kind] ||= []).push(fn); },
    listeners: {},
    matchMedia: (query) => ({ matches: window.reducedMotion === true, media: query }),
    innerWidth: 1440,
    innerHeight: 900,
    reducedMotion: false,
    location: { protocol: 'http:', host: 'localhost:8765', search: '' },
    navigator: { mediaDevices: null },
    dispatch(kind, event = {}) {
      for (const fn of window.listeners[kind] || []) fn({ preventDefault() {}, ...event });
    },
  };

  // `navigator` is a getter-only global in current node, so plain assignment
  // throws. defineProperty works for all of them and says what it is doing.
  const put = (name, value) =>
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  put('document', document);
  put('window', window);
  put('navigator', window.navigator);
  put('location', window.location);
  put('WebSocket', FakeSocket);
  put('MediaRecorder', undefined);
  return { document, window, byId };
}
