const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/app.js'), 'utf8');
const start = source.indexOf('      const strategy = form.querySelector');
const end = source.indexOf('    });\n    const duration', start);
assert.ok(start >= 0 && end > start);
const wiring = source.slice(start, end);

function control(value, extra = {}) {
  return {value, defaultValue: value, type: 'textarea', handlers: {},
    addEventListener(event, handler) { this.handlers[event] = handler; }, ...extra};
}

function setup({current = true, busy = false, restored = false} = {}) {
  const strategy = control('planned_scene');
  const direction = control('Saved pose');
  if (restored) direction.value = 'Unsaved restored pose';
  const image = control('Saved scene');
  const character = control('', {type:'checkbox', checked:true, defaultChecked:true});
  const render = {disabled:busy, dataset:{planCurrent:String(current)}};
  const save = {disabled:busy};
  const planButton = {disabled:busy};
  const pending = {hidden:true};
  const form = {querySelector(selector) {
    return ({'[name="strategy"]':strategy, '[name="scene_direction"]':direction,
      'button[formaction]':save, '[data-plan-render]':render})[selector];
  }};
  const content = {querySelectorAll:() => [direction,image,character], querySelector(selector) {
    return selector === '[data-plan-pending]' ? pending : planButton;
  }};
  vm.runInNewContext(wiring, {form,content});
  return {strategy,direction,image,character,render,save,planButton,pending};
}

test('current plan renders, source edits gate plan/render and undo unlocks', () => {
  for (const sourceName of ['direction','image','character']) {
    const state = setup();
    assert.equal(state.render.disabled, false);
    const input = state[sourceName];
    if (input.type === 'checkbox') input.checked = false;
    else input.value = 'Changed';
    input.handlers.input();
    assert.equal(state.render.disabled, true);
    assert.equal(state.planButton.disabled, true);
    assert.equal(state.pending.hidden, false);
    if (input.type === 'checkbox') input.checked = input.defaultChecked;
    else input.value = input.defaultValue;
    input.handlers.change();
    assert.equal(state.render.disabled, false);
    assert.equal(state.planButton.disabled, false);
    assert.equal(state.pending.hidden, true);
  }
});

test('poll-restored edits compare against server defaults, not restored values', () => {
  const state = setup({restored:true});
  assert.equal(state.render.disabled, true);
  assert.equal(state.planButton.disabled, true);
  assert.equal(state.pending.hidden, false);
});

test('missing plan blocks only new mode; both previous modes remain usable', () => {
  const state = setup({current:false});
  assert.equal(state.render.disabled, true);
  assert.equal(state.planButton.disabled, false);
  for (const value of ['reference_scene','masked']) {
    state.strategy.value = value;
    state.strategy.handlers.change();
    assert.equal(state.render.disabled, false);
    assert.equal(state.direction.disabled, value === 'masked');
    assert.equal(state.save.disabled, value === 'masked');
  }
});

test('strategy changes or restored edits cannot unlock busy jobs', () => {
  const state = setup({busy:true});
  for (const value of ['planned_scene','reference_scene','masked']) {
    state.strategy.value = value;
    state.strategy.handlers.change();
    assert.equal(state.render.disabled, true);
    assert.equal(state.planButton.disabled, true);
    assert.equal(state.save.disabled, true);
  }
});
