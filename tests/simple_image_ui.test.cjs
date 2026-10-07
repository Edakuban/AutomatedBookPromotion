const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/app.js'), 'utf8');
const start = source.indexOf('function wireSimpleImageForms(scope) {');
const end = source.indexOf('\nwireSimpleImageForms(document);', start);
assert.ok(start >= 0 && end > start);

function setup(castSpecs, {busy = false} = {}) {
  const cast = castSpecs.map(([checked, reference]) => ({checked,
    dataset: {hasReference: String(reference)}, handlers: {}, validity: '',
    addEventListener(type, handler) { this.handlers[type] = handler; },
    setCustomValidity(value) { this.validity = value; },
  }));
  const referenceButton = {disabled: busy};
  const hint = {textContent: '', hidden: true};
  const form = {dataset: {simpleImageBusy: String(busy)},
    addEventListener() {},
    querySelectorAll: () => cast,
    querySelector: selector => ({'[data-simple-image-reference]': referenceButton,
      '[data-simple-image-hint]': hint})[selector] || null};
  const scope = {querySelectorAll: () => [form]};
  vm.runInNewContext(source.slice(start, end) + '\nwireSimpleImageForms(scope);', {scope});
  return {cast, form, referenceButton, hint, sync: () => cast[0]?.handlers.change()};
}

test('no stored plan or prior image is needed; reference mode only checks selected references', () => {
  const env = setup([[true, true], [false, false]]);
  assert.equal(env.referenceButton.disabled, false);
  assert.equal(env.hint.hidden, true);
  env.cast[1].checked = true;
  env.sync();
  assert.equal(env.referenceButton.disabled, true);
  assert.match(env.hint.textContent, /jede ausgewählte Figur ein Referenzbild/);
  env.cast[1].checked = false;
  env.sync();
  assert.equal(env.referenceButton.disabled, false);
});

test('environment-only scene works without cast, while references require a cast', () => {
  const env = setup([[false, true]]);
  assert.equal(env.referenceButton.disabled, true);
  assert.match(env.hint.textContent, /nur die Szene/);
  env.cast[0].checked = true;
  env.sync();
  assert.equal(env.referenceButton.disabled, false);
});

test('five selected figures are invalid, undo restores validity', () => {
  const env = setup(Array.from({length: 5}, () => [true, true]));
  assert.equal(env.referenceButton.disabled, true);
  assert.ok(env.cast.every(input => input.validity));
  env.cast[4].checked = false;
  env.sync();
  assert.equal(env.referenceButton.disabled, false);
  assert.ok(env.cast.every(input => !input.validity));
});

test('cast edits cannot unlock a busy or currently submitting reference action', () => {
  const busy = setup([[true, true]], {busy: true});
  busy.sync();
  assert.equal(busy.referenceButton.disabled, true);
  const pending = setup([[true, true]]);
  pending.form.dataset.simpleImageSubmitting = 'true';
  pending.sync();
  assert.equal(pending.referenceButton.disabled, true);
  delete pending.form.dataset.simpleImageSubmitting;
  pending.sync();
  assert.equal(pending.referenceButton.disabled, false);
});

test('AJAX includes the explicitly chosen named button even after it is disabled', () => {
  const start = source.indexOf('      const action = event.submitter?.hasAttribute');
  const end = source.indexOf('      const response = await fetch(action, {', start);
  assert.ok(start > 0 && end > start);
  for (const mode of ['plain', 'text', 'references']) {
    const values = [];
    class FormData { constructor(form) { values.push(['revision', form.revision]); }
      append(name, value) { values.push([name, value]); } }
    const form = {revision: 7, action: '/image/create'};
    const event = {submitter: {name: 'mode', value: mode, disabled: true, hasAttribute: () => false}};
    vm.runInNewContext(source.slice(start, end), {form, event, FormData});
    assert.deepEqual(values, [['revision', 7], ['mode', mode]]);
  }
});
