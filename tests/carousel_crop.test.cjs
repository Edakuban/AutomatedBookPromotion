const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(width = 900, height = 1600, unavailable = false) {
  class Element {
    constructor() { this.handlers = {}; this.style = {}; this.hidden = false; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    dispatchEvent(event) { this.handlers[event.type]?.(event); form.handlers.input?.(event); }
    setAttribute(name, value) { this[name] = value; }
    closest() { return this.label; }
  }
  const x = Object.assign(new Element(), {value:'0.5', label:new Element()});
  const y = Object.assign(new Element(), {value:'0.5', label:new Element()});
  const frame = Object.assign(new Element(), {
    getBoundingClientRect: () => ({width:300, height:375}),
    setPointerCapture() {}, classList:{add() {}, remove() {}},
  });
  const image = Object.assign(new Element(), {naturalWidth:width, naturalHeight:height});
  const center = new Element(), pending = new Element();
  const button = Object.assign(new Element(), {disabled:unavailable});
  const prepare = Object.assign(new Element(), {querySelector:() => button});
  const form = Object.assign(new Element(), {
    elements:{namedItem: name => name === 'focus_x' ? x : y},
    querySelector: selector => ({'[data-crop-frame]':frame, '[data-crop-image]':image, '[data-crop-center]':center})[selector],
  });
  const root = {querySelector: selector => ({'[data-carousel-crop]':form, '[data-carousel-prepare]':prepare, '[data-crop-pending]':pending})[selector]};
  let aborted = false, disconnected = false;
  const window = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/carousel_crop.js'), 'utf8'), {
    window, Event:class {constructor(type) {this.type = type;}},
    AbortController:class {signal = {}; abort() {aborted = true;}},
    ResizeObserver:class {observe() {} disconnect() {disconnected = true;}},
  });
  const cleanup = window.bookpromoCrop.wire(root);
  return {x, y, frame, image, center, prepare, button, pending, cleanup, cleaned:() => aborted && disconnected};
}

test('portrait drag is clamped, matches object-position, blocks prepare until reset', () => {
  const {x, y, frame, image, button, pending, center, prepare, cleanup, cleaned} = setup();
  assert.equal(x.label.hidden, true);
  assert.equal(y.label.hidden, false);
  assert.equal(button.disabled, false);
  frame.handlers.pointerdown({pointerId:1, pointerType:'mouse', button:0, clientX:100, clientY:100, preventDefault() {}});
  frame.handlers.pointermove({pointerId:1, clientX:150, clientY:1000});
  assert.equal(x.value, '0.5');
  assert.equal(y.value, '0');
  assert.equal(image.style.objectPosition, '50% 0%');
  assert.equal(button.disabled, true);
  assert.equal(pending.hidden, false);
  let rejected = false;
  prepare.handlers.submit({preventDefault() {rejected = true;}, stopImmediatePropagation() {}});
  assert.equal(rejected, true);
  center.handlers.click();
  assert.equal(y.value, '0.5');
  assert.equal(button.disabled, false);
  assert.equal(pending.hidden, true);
  cleanup(); assert.equal(cleaned(), true);
});

test('landscape reveals horizontal axis and never unlocks unavailable upload', () => {
  const {x, y, frame, center, button} = setup(1600, 900, true);
  assert.equal(x.label.hidden, false);
  assert.equal(y.label.hidden, true);
  frame.handlers.pointerdown({pointerId:2, pointerType:'touch', clientX:200, clientY:100, preventDefault() {}});
  frame.handlers.pointermove({pointerId:2, clientX:-1000, clientY:500});
  assert.equal(x.value, '1');
  assert.equal(y.value, '0.5');
  center.handlers.click();
  assert.equal(button.disabled, true);
});
