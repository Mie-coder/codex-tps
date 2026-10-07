/* Offline regression check: node tests/test_identity.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../scripts/overlay.js'), 'utf8');
const current = '00000000-0000-4000-8000-000000000001';
const other = '00000000-0000-4000-8000-000000000002';

function identity(composers = [], editors = [], sidebar = []) {
  const rows = new Map([
    ['[data-above-composer-conversation-id]', composers],
    ['[data-conversation-id]', editors],
    ['[data-app-action-sidebar-thread-active="true"]', sidebar],
  ]);
  const document = {
    querySelectorAll(selector) {
      return (rows.get(selector) || []).map(([id, hidden]) => ({
        getAttribute: () => id,
        closest: () => hidden ? {} : null,
      }));
    },
  };
  // No DOM mutation methods are provided: identity discovery must be read-only.
  return vm.runInNewContext(`(${source})(null)`, {document, window: {}});
}

assert.equal(identity([[current, false]]), current); // Sidebar can be collapsed.
assert.equal(identity([[other, true], ['local:' + current, false]], [[other, true]]), current);
assert.equal(identity([[current, false], [current, false]]), current);
assert.equal(identity([[current, false], [other, false]]), ''); // Never guess between chats.
assert.equal(identity([], [], [['local:' + current, false]]), current);
assert.equal(identity([['not-a-session-id', false]]), '');
console.log('Identity regression check passed (6 cases).');

// Exercise the actual private lookup against the current native marker shape.
// The test-only return avoids creating UI or requiring a browser dependency.
const lookupSource = source.replace('if (payload === null) return activeThread();',
  'if (payload === null) return finalNode;');
function node(attributes = {}, children = [], hidden = false) {
  return {
    getAttribute: name => attributes[name] ?? null,
    matches: selector => selector.split(',').some(s => s.slice(1, -1) in attributes),
    closest(selector) {
      if (selector.includes('data-app-shell-active-page')) return hidden ? {} : null;
      return this.matches(selector) ? this : null;
    },
    querySelectorAll(selector) { return children.filter(n => n.matches(selector)); },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
}
function lookup(nodes, turn) {
  const document = {querySelectorAll: selector => nodes.filter(n => n.matches(selector))};
  return vm.runInNewContext(`(${lookupSource})(null)`, {document, window:{}})(turn);
}
const reply = node({'data-local-conversation-final-assistant':'true'});
const nativeTurn = node({'data-content-search-turn-key':current}, [reply]);
const userOnly = node({'data-content-search-turn-key':current});
const wrongTurn = node({'data-content-search-turn-key':other}, [reply]);
assert.equal(lookup([userOnly, nativeTurn], {turn_id:current}), reply);
assert.equal(lookup([wrongTurn], {turn_id:current}), null);
assert.equal(lookup([node({'data-content-search-turn-key':current}, [reply], true)], {turn_id:current}), null);
assert.equal(lookup([node({'data-content-search-turn-key':current}, [reply, reply])], {turn_id:current}), null);
const encodedReply = node({'data-local-conversation-item-target-ids':'msg%3Aid',
  'data-local-conversation-final-assistant':'true'});
assert.equal(lookup([encodedReply], {turn_id:current, final_ids:['msg:id']}), encodedReply);
console.log('Reply lookup regression check passed (5 cases).');
