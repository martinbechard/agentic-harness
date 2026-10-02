const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('dashboard renders authoritative snapshots, uncertainty, and fetch failures', async () => {
  const element = (tagName) => ({
    tagName, textContent: '', children: [],
    append(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
  });
  const ids = Object.fromEntries(['revision', 'process', 'items', 'invocations', 'coverage', 'traces']
    .map(id => ['#' + id, element('section')]));
  const snapshot = {
    as_of: 'now', runtime_freshness: 'fresh', revision: '1234567890123456',
    run_observation: {state: 'Available', mode: 'SOLO', admission_open: true},
    capability_blockers: [], generation_configuration_error: null,
    items: [{item_id: '<img src=x>', state: 'Running', usage: {generated_tokens: 7, ceiling: null}}],
    invocations: [{role: 'coordinator', item_id: null, outcome: 'returned'}],
    trace_span_count: 2, traces: [{name: 'turn', item_id: null, trace_id: 'trace'}],
    uncertainty: [], telemetry_receipt_binding: 'content_hash',
  };
  let response = {ok: true, json: async () => snapshot};
  let interval;
  const context = vm.createContext({
    document: {createElement: element, querySelector: id => ids[id]},
    fetch: async url => { assert.equal(url, '/api/snapshot'); return response; },
    setInterval: (callback, ms) => { assert.equal(ms, 15000); interval = callback; },
  });
  const filename = path.resolve(__dirname, '../src/backlog_harness/static/dashboard.js');
  new vm.Script(fs.readFileSync(filename, 'utf8'), {filename}).runInContext(context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(interval, context.refresh);
  assert.match(ids['#revision'].textContent, /Revision 123456789012$/);
  assert.match(ids['#process'].textContent, /Admission open/);
  assert.equal(ids['#items'].children[0].children[0].textContent, '<img src=x>');
  assert.equal(ids['#items'].children[0].children[3].textContent, 'Unknown');
  assert.match(ids['#invocations'].children[0].textContent, /Run-level/);
  assert.equal(ids['#traces'].children[0].children[1].textContent, 'Run-level');

  snapshot.run_observation = {state: 'Holding', mode: null, admission_open: false};
  snapshot.generation_configuration_error = 'Invalid config';
  snapshot.capability_blockers = ['missing usage'];
  snapshot.telemetry_receipt_binding = 'legacy_count_only';
  snapshot.invocations[0].item_id = 'one';
  snapshot.traces[0].item_id = 'one';
  await context.refresh();
  assert.match(ids['#process'].textContent, /Unknown mode.*Admission closed.*1 current blockers.*invalid/);
  assert.match(ids['#coverage'].textContent, /Historical receipts/);
  assert.match(ids['#invocations'].children[0].textContent, /one/);
  assert.equal(ids['#traces'].children.length, 1); // Refresh replaces, never duplicates.
  assert.equal(ids['#traces'].children[0].children[1].textContent, 'one');

  snapshot.run_observation.admission_open = null;
  snapshot.items = []; snapshot.invocations = []; snapshot.traces = [];
  await context.refresh();
  assert.match(ids['#process'].textContent, /Admission unknown/);
  for (const id of ['#items', '#invocations', '#traces']) assert.equal(ids[id].children.length, 0);

  response = {ok: false};
  await context.refresh();
  assert.equal(ids['#revision'].textContent, 'Evidence unavailable');
  response = {ok: true, json: async () => { throw Error('Malformed snapshot'); }};
  await context.refresh();
  assert.equal(ids['#revision'].textContent, 'Malformed snapshot');
});
