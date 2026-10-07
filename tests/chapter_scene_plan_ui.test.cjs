const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/app.js'), 'utf8');
const start = source.indexOf('const chapterImageMethods =');
const end = source.indexOf('// Chapter and final-teaser queue forms', start);
assert.ok(start >= 0 && end > start);
const wiring = source.slice(start, end);

function setup({current = true, busy = false, rows = 1} = {}) {
  const methods = [];
  const chapterRows = [];
  const states = [];
  const control = (value, extra = {}) => ({value, defaultValue: value, type: 'textarea', ...extra});
  const listener = target => Object.assign(target, {handlers: {}, addEventListener(type, handler) {
    this.handlers[type] = handler;
  }});
  function imageForm(row, strategy = 'planned_scene') {
    const method = listener(control(strategy));
    const render = {disabled: busy};
    const message = {hidden: true, textContent: 'Update the saved plan.', dataset: {}};
    const form = listener({dataset: {planReady: String(current && !busy), legacyReady: String(!busy)},
      querySelector(selector) {
        return ({'[data-chapter-method]': method, '[data-chapter-method-render]': render,
          '[data-chapter-method-message]': message})[selector];
      }, closest: () => row});
    methods.push(form);
    return {form, method, render, message};
  }
  for (let i = 0; i < rows; i++) {
    const direction = control('Saved direction');
    const image = control('Saved scene');
    const cast = control('character', {type: 'checkbox', checked: true, defaultChecked: true});
    const editorCast = control('character', {type: 'checkbox', checked: true, defaultChecked: true});
    const noImageCast = control('without-image', {type: 'checkbox', checked: true, defaultChecked: true});
    const button = {disabled: busy, dataset: {}};
    const pending = {hidden: true};
    const generation = {querySelector: selector => selector === 'button[type="submit"]' ? button : pending};
    const row = listener({querySelector: () => generation, querySelectorAll(selector) {
      return selector === '[data-chapter-plan-inputs] [name="character_id"]'
        ? [editorCast, noImageCast] : [direction, image, cast, editorCast, noImageCast];
    }});
    for (const input of [direction, image, cast, editorCast, noImageCast]) {
      input.matches = () => input === cast;
      input.dispatchEvent = event => row.handlers[event.type]({target: input});
    }
    const optimize = imageForm(row);
    const regen = imageForm(row, 'scene_plan');
    chapterRows.push(row);
    states.push({row, direction, image, cast, editorCast, noImageCast, button, pending, optimize, regen});
  }
  const bulk = imageForm(null);
  const document = {querySelectorAll: selector => selector === '[data-chapter-image-method]' ? methods : chapterRows};
  vm.runInNewContext(wiring, {document, Event: class {constructor(type) {this.type = type;}}});
  return {states, bulk};
}

test('dirty cast, direction and image gate planned rendering/generation; undo unlocks', () => {
  for (const name of ['cast', 'direction', 'image']) {
    const {states, bulk} = setup();
    const state = states[0];
    assert.equal(state.optimize.render.disabled, false);
    const input = state[name];
    if (input.type === 'checkbox') input.checked = false;
    else input.value = 'Changed';
    input.dispatchEvent({type: 'change'});
    assert.equal(state.optimize.render.disabled, true);
    assert.equal(state.regen.render.disabled, true);
    assert.equal(state.button.disabled, true);
    assert.equal(state.pending.hidden, false);
    assert.equal(bulk.render.disabled, true);
    assert.match(state.optimize.message.textContent, /speichern/);
    state.optimize.method.value = 'masked';
    state.optimize.method.handlers.change();
    assert.equal(state.optimize.render.disabled, false);
    assert.equal(state.optimize.message.hidden, true);
    if (input.type === 'checkbox') input.checked = input.defaultChecked;
    else input.value = input.defaultValue;
    input.dispatchEvent({type: 'change'});
    state.optimize.method.value = 'planned_scene';
    state.optimize.method.handlers.change();
    assert.equal(state.optimize.render.disabled, false);
    assert.equal(state.regen.render.disabled, false);
    assert.equal(state.button.disabled, false);
    assert.equal(state.pending.hidden, true);
    assert.equal(bulk.render.disabled, false);
  }
});

test('cast mirroring preserves selected profiles without reference images', () => {
  const state = setup().states[0];
  state.cast.checked = false;
  state.cast.dispatchEvent({type: 'change'});
  assert.equal(state.editorCast.checked, false);
  assert.equal(state.noImageCast.checked, true);
});

test('missing or stale plan blocks planned methods while legacy rendering remains available', () => {
  const {states, bulk} = setup({current: false});
  const state = states[0];
  assert.equal(state.optimize.render.disabled, true);
  assert.equal(state.regen.render.disabled, true);
  assert.equal(state.button.disabled, false);
  assert.equal(bulk.render.disabled, true);
  for (const method of [state.optimize, state.regen, bulk]) {
    method.method.value = 'masked';
    method.method.handlers.change();
    assert.equal(method.render.disabled, false);
  }
});

test('busy read-only chapters cannot be unlocked by method changes or undo', () => {
  const {states, bulk} = setup({busy: true});
  const state = states[0];
  for (const method of [state.optimize, state.regen, bulk]) {
    for (const value of ['masked', 'planned_scene', 'scene_plan']) {
      method.method.value = value;
      method.method.handlers.change();
      assert.equal(method.render.disabled, true);
      assert.equal(state.button.disabled, true);
    }
  }
  state.direction.value = 'Changed';
  state.direction.dispatchEvent({type: 'input'});
  state.direction.value = state.direction.defaultValue;
  state.direction.dispatchEvent({type: 'input'});
  assert.equal(state.button.disabled, true);
});

test('any dirty chapter blocks bulk planned rendering without locking other idle chapters', () => {
  const {states, bulk} = setup({rows: 2});
  states[1].direction.value = 'Changed';
  states[1].direction.dispatchEvent({type: 'input'});
  assert.equal(bulk.render.disabled, true);
  assert.equal(states[0].optimize.render.disabled, false);
  assert.equal(states[0].button.disabled, false);
  assert.equal(states[1].optimize.render.disabled, true);
  bulk.method.value = 'masked';
  bulk.method.handlers.change();
  assert.equal(bulk.render.disabled, false);
});

test('disabled planned render cannot be submitted through an alternate submission path', () => {
  const state = setup({current: false}).states[0];
  const event = {prevented: false, preventDefault() {this.prevented = true;}};
  state.optimize.form.handlers.submit(event);
  assert.equal(event.prevented, true);
});
