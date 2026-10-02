// Dependency-free regression checks for POST/redirect and background reloads.
// Run: node --test tools/check_page_position.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import test from 'node:test';

const script = readFileSync(new URL('../src/bookpromo/static/page_position.js', import.meta.url), 'utf8');
function page({storage = new Map(), shift = 0, disabled = false, removed = false,
               navigation = 'navigate', hash = '', denied = false, dialog = false, lightbox = false} = {}) {
  const listeners = {};
  const window = {
    location: {pathname: '/books/local/book/teaser', href: 'http://localhost/books/local/book/teaser',
      origin: 'http://localhost', hash},
    scrollX: 0, scrollY: 0, innerHeight: 800,
    sessionStorage: {
      getItem: key => {if (denied) throw Error(); return storage.get(key) ?? null;},
      setItem: (key, value) => {if (denied) throw Error(); storage.set(key, value);},
      removeItem: key => storage.delete(key),
    },
    performance: {getEntriesByType: () => [{type: navigation}]},
    addEventListener: (name, fn) => {listeners[name] = fn;},
    requestAnimationFrame: fn => fn(),
    scrollTo: ({left, top}) => {window.scrollX = left; window.scrollY = top;},
  };
  const document = {activeElement: null, addEventListener: (name, fn) => {listeners[name] = fn;}};
  class Element {
    constructor(tag, parent, props = {}) {
      Object.assign(this, {tagName: tag.toUpperCase(), parent, children: [], dataset: {},
        id: '', name: '', open: false, disabled: false, top: 1200 + shift}, props);
      parent?.children.push(this);
    }
    matches(selector) {
      if (selector.includes(',')) return selector.split(',').some(part => this.matches(part.trim()));
      if (selector === '[data-page-position-anchor]') return !!this.dataset.pagePositionAnchor;
      if (selector === '[data-page-position-key]') return !!this.dataset.pagePositionKey;
      if (selector === '[data-teaser-dialog-open]') return !!this.dataset.teaserDialogOpen;
      if (selector === '[data-teaser-image-open]') return !!this.dataset.teaserImageOpen;
      if (selector === 'a[href]') return this.tagName === 'A' && !!this.href;
      if (selector === 'details[open]') return this.tagName === 'DETAILS' && this.open;
      if (selector === 'dialog:not([open])') return this.tagName === 'DIALOG' && !this.open;
      return this.tagName === selector.toUpperCase();
    }
    closest(selector) {return this.matches(selector) ? this : this.parent?.closest(selector);}
    contains(element) {return element === this || this.children.some(child => child.contains(element));}
    querySelectorAll(selector) {
      const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
      return selector === '*' ? descendants : descendants.filter(child =>
        selector.split(',').some(part => child.matches(part.trim())));
    }
    querySelector(selector) {return this.querySelectorAll(selector)[0] || null;}
    getBoundingClientRect() {const top = this.top - window.scrollY; return {top, bottom: top + 40};}
    getClientRects() {
      for (let node = this; node; node = node.parent) {
        if (node.matches('dialog:not([open])') ||
            (node.tagName === 'DETAILS' && !node.open && node !== this)) return [];
      }
      return [this.getBoundingClientRect()];
    }
    setAttribute(name, value) {this[name] = value;}
    focus(options) {document.activeElement = this; this.focusOptions = options;}
  }
  const main = new Element('main', null, {id: 'content'});
  document.getElementById = id => id === 'content' ? main : main.querySelectorAll('*').find(node => node.id === id);
  const row = new Element('tr', main, {dataset: {pagePositionAnchor: 'chapter-6'}});
  const details = new Element('details', row);
  const opener = new Element('button', row, {dataset: {teaserDialogOpen: 'prompt-6'}, top: 1320 + shift});
  const editor = dialog ? new Element('dialog', row, {id: 'prompt-6', open: true}) : null;
  const form = new Element('form', editor || details, {action: 'http://localhost/chapters/6/optimize'});
  const button = removed ? null : new Element('button', form, {disabled, top: 1320 + shift});
  const link = new Element('a', row, {href: 'http://localhost/books/local/book/teaser?view=image', top: 1320 + shift});
  const image = new Element('a', row, {href: 'http://localhost/images/6', top: 1320 + shift,
    dataset: {teaserImageOpen: 'true'}});
  const imageDialog = lightbox ? new Element('dialog', main, {id: 'chapter-image-lightbox', open: true}) : null;
  const close = imageDialog ? new Element('button', imageDialog) : null;
  runInNewContext(script, {window, document, URL, Map, Date, JSON, Number});
  return {window, document, row, details, opener, editor, form, button, link, image, close, storage,
    event: (name, extra = {}) => listeners[name]?.({persisted: false, ...extra})};
}
function submit(old) {
  old.window.scrollY = 1200;
  old.details.open = true;
  old.document.activeElement = old.button;
  old.event('submit', {target: old.form, submitter: old.button});
  old.event('pagehide');
}

test('POST restores focus, expanded details, and viewport offset after layout changes', () => {
  const old = page(); submit(old);
  const next = page({storage: old.storage, shift: 500}); next.event('pageshow');
  assert.equal(next.details.open, true);
  assert.equal(next.window.scrollY, 1700);
  assert.equal(next.document.activeElement, next.button);
  assert.equal(next.button.focusOptions.preventScroll, true);
  assert.equal(old.storage.size, 0);
});
for (const option of ['disabled', 'removed']) {
  test(`${option} button falls back to its chapter`, () => {
    const old = page(); submit(old);
    const next = page({storage: old.storage, shift: 500, [option]: true}); next.event('pageshow');
    assert.equal(next.window.scrollY, 1700);
    assert.equal(next.document.activeElement, next.row);
  });
}
test('saving a prompt dialog returns to the visible opener', () => {
  const old = page({dialog: true}); submit(old);
  const next = page({storage: old.storage, shift: 500, dialog: true});
  next.editor.open = false; next.event('pageshow');
  assert.equal(next.document.activeElement, next.opener);
  assert.equal(next.window.scrollY, 1700);
});
test('same-page link preserves its viewport position and focus', () => {
  const old = page(); old.window.scrollY = 1200;
  old.event('click', {target: old.link, button: 0}); old.event('pagehide');
  const next = page({storage: old.storage, shift: 500}); next.event('pageshow');
  assert.equal(next.document.activeElement, next.link);
  assert.equal(next.window.scrollY, 1700);
});
test('background reload of an image lightbox returns to the actual clicked image', () => {
  const old = page({lightbox: true}); old.window.scrollY = 1200;
  old.event('click', {target: old.image, button: 0});
  old.document.activeElement = old.close; old.event('pagehide');
  const next = page({storage: old.storage, shift: 500, navigation: 'reload'}); next.event('pageshow');
  assert.equal(next.document.activeElement, next.image);
  assert.equal(next.window.scrollY, 1700);
});
test('background reload restores scroll without forcing offscreen button focus', () => {
  const old = page(); old.window.scrollY = 2000; old.event('pagehide');
  const next = page({storage: old.storage, navigation: 'reload'}); next.event('pageshow');
  assert.equal(next.window.scrollY, 2000);
  assert.equal(next.document.activeElement, null);
});
test('POST redirect fragment does not override action position', () => {
  const old = page(); submit(old);
  const next = page({storage: old.storage, hash: '#quotes-title'}); next.event('pageshow');
  assert.equal(next.document.activeElement, next.button);
});
test('regular visits, back navigation, and explicit fragment reload keep native behavior', () => {
  for (const options of [{}, {navigation: 'back_forward'}, {navigation: 'reload', hash: '#section'}]) {
    const old = page(); old.window.scrollY = 1200; old.event('pagehide');
    const next = page({storage: old.storage, ...options}); next.event('pageshow');
    assert.equal(next.window.scrollY, 0);
  }
});
test('storage disabled is harmless', () => {
  const old = page({denied: true}); submit(old); old.event('pageshow');
});
test('expired state is discarded', () => {
  const old = page(); submit(old);
  for (const [key, value] of old.storage) {
    old.storage.set(key, JSON.stringify({...JSON.parse(value), time: 0}));
  }
  const next = page({storage: old.storage}); next.event('pageshow');
  assert.equal(next.window.scrollY, 0);
});
