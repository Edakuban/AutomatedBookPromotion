const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Small DOM double: exercise the production controller, not a copy of its logic.
function setup() {
  class Element {
    constructor() { this.dataset = {}; this.children = []; this.hidden = false; this.isConnected = true; this.handlers = {}; }
    append(child) { this.children.push(child); }
    prepend(child) { this.children.unshift(child); }
    setAttribute(name, value) { this[name] = value; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    querySelector(selector) {
      if (selector === '[data-unsaved-message]') return this.children.find(child => 'unsavedMessage' in child.dataset);
      if (selector === 'button') return this.children.find(child => child.tag === 'button');
      if (selector === '[data-unsaved-status]') return this.children.find(child => 'unsavedStatus' in child.dataset) || null;
      return null;
    }
    contains(child) { return child === this || this.children.includes(child); }
  }
  const form = new Element();
  const other = new Element();
  form.elements = [{name:'title', type:'text', value:'Original'}, {name:'revision', type:'hidden', value:'1'}];
  other.elements = [{name:'enabled', type:'checkbox', value:'on', checked:false}];
  form.hasAttribute = other.hasAttribute = name => name === 'data-unsaved-form';
  form.method = other.method = 'post';
  const main = new Element();
  const document = {
    handlers: {}, querySelectorAll: () => [form, other],
    createElement: tag => Object.assign(new Element(), {tag}),
    getElementById: () => main,
    contains: element => [form, other].includes(element),
    addEventListener(name, handler) { this.handlers[name] = handler; },
  };
  let reloads = 0;
  let confirmResult = false;
  let confirms = 0;
  const window = {
    handlers: {}, location: {reload: () => { reloads++; }},
    confirm: () => { confirms++; return confirmResult; },
    addEventListener(name, handler) { this.handlers[name] = handler; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/unsaved_changes.js'), 'utf8'),
    {document, window, queueMicrotask});
  return {form, other, main, document, window, guard:window.bookpromoUnsaved,
    reloads: () => reloads, confirms: () => confirms, confirm: result => {confirmResult = result;}};
}

test('changing, undoing, hidden revisions and added fields', () => {
  const {form, guard} = setup();
  assert.equal(guard.hasChanges(), false);
  form.elements[1].value = '2';
  assert.equal(guard.hasChanges(), false);
  form.elements[0].value = 'Edited';
  guard.refresh(form);
  assert.equal(guard.hasChanges(), true);
  assert.equal(form.querySelector('[data-unsaved-status]').hidden, false);
  form.elements[0].value = 'Original';
  assert.equal(guard.hasChanges(), false);
  form.elements.push({name:'chapter', type:'text', value:'New chapter'});
  assert.equal(guard.hasChanges(), true);
});

test('beforeunload and automatic reload protect unsaved values', () => {
  const env = setup();
  env.form.elements[0].value = 'Edited';
  const event = {preventDefault() { this.prevented = true; }};
  env.window.handlers.beforeunload(event);
  assert.equal(event.prevented, true);
  assert.equal(env.guard.reload(), false);
  assert.equal(env.reloads(), 0);
  assert.equal(env.main.children[0].querySelector('button').hidden, false);
  env.guard.accept(env.form);
  assert.equal(env.guard.reload(), true);
  assert.equal(env.reloads(), 1);
});

test('native submit warns about other forms but not the form being saved', async () => {
  const env = setup();
  env.form.elements[0].value = 'Edited';
  const event = {target:env.form, preventDefault() {this.defaultPrevented = true;}, stopImmediatePropagation() {this.stopped = true;}};
  env.document.handlers.submit(event);
  await Promise.resolve();
  assert.equal(env.confirms(), 0);
  const unload = {preventDefault() {this.prevented = true;}};
  env.window.handlers.beforeunload(unload);
  assert.equal(unload.prevented, undefined);
  env.window.handlers.pageshow();
  env.other.elements[0].checked = true;
  env.document.handlers.submit(event);
  assert.equal(event.defaultPrevented, true);
  assert.equal(event.stopped, true);
  assert.equal(env.confirms(), 1);
});

test('successful AJAX save does not clear edits made while request was running', () => {
  const {form, guard} = setup();
  form.elements[0].value = 'Sent';
  const sent = guard.capture(form);
  form.elements[0].value = 'Edited while saving';
  guard.accept(form, sent);
  assert.equal(guard.hasChanges(), true);
  form.elements[0].value = 'Sent';
  assert.equal(guard.hasChanges(), false);
});

test('file selections and modal cancel remain protected without browser storage', () => {
  const env = setup();
  env.form.elements.push({name:'file', type:'file', files:[]});
  env.guard.accept(env.form);
  env.form.elements.at(-1).files = [{name:'cover.png',size:120,lastModified:123}];
  assert.equal(env.guard.hasChanges(), true);
  assert.equal(env.guard.confirmDiscard(), false);
  env.confirm(true);
  assert.equal(env.guard.confirmDiscard(), true);
  env.guard.forget({contains: form => form === env.form});
  assert.equal(env.guard.hasChanges(), false);
});

test('workshop initialization accepts normalized audio without discarding restored user edits', () => {
  const appSource = fs.readFileSync(path.join(__dirname, '../src/bookpromo/static/app.js'), 'utf8');
  const start = appSource.indexOf('  function wireReelFragment() {');
  const end = appSource.indexOf('  async function loadWorkshop(', start);
  assert.ok(start >= 0 && end > start);
  const initializeSource = appSource.slice(start, end) + '\nwireReelFragment();';

  for (const restored of [false, true]) {
    const env = setup();
    const forms = [env.form, env.other];
    forms.forEach(form => {
      form.hasAttribute = name => name === 'data-reel-action';
      form.addEventListener = () => {};
    });
    env.form.elements = [{name:'image_prompt', type:'text', value:'Saved scene'}];
    env.other.elements = [
      {name:'audio_start_seconds', type:'number', value:'0.0'},
      {name:'duration_seconds', type:'range', value:'20.0'},
    ];
    forms.forEach(form => env.guard.accept(form));
    if (restored) {
      env.form.elements[0].value = 'User edit restored during polling';
      env.guard.refresh(env.form);
    }
    const content = {
      querySelector: () => null,
      querySelectorAll: selector => selector === 'form[data-reel-action]' ? forms
        : selector === 'form[data-reel-dirty="true"]' ? forms.filter(form => form.dataset.reelDirty === 'true') : [],
    };
    env.window.bookpromoCrop = {wire:() => () => {}};
    vm.runInNewContext(initializeSource, {
      content, workshopUrl:'http://localhost/workshop', location:{href:'http://localhost/'},
      URL, sessionStorage:{getItem:() => null}, window:env.window,
      wireAudioEditor:() => {
        // Production syncSelection formats numeric controls before the baseline loop.
        env.other.elements[0].value = '0';
        env.other.elements[1].value = '20';
        return () => {};
      },
      wireSimpleImageForms:() => {},
    });
    assert.equal(env.other.dataset.reelDirty, undefined, 'programmatic audio formatting stays clean');
    assert.equal(env.guard.hasChanges(), restored);
    assert.equal(env.form.dataset.reelDirty, restored ? 'true' : undefined,
      'real restored edits remain protected');
  }
});
