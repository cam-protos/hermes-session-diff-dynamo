import assert from 'node:assert/strict';
import { diffRows } from '../src/diff-rows.js';

const rows = diffRows('--- a/file\n+++ b/file\n@@ -2,2 +2,2 @@\n same\n-old\n+new\n\\ No newline at end of file\n@@ -9,0 +10,2 @@\n+++ literal\n+\n');
assert.deepEqual(rows.filter(r => r.kind === 'context'), [{ kind: 'context', before: 2, after: 2, text: ' same' }]);
assert.deepEqual(rows.filter(r => r.kind === 'remove'), [{ kind: 'remove', before: 3, text: '-old' }]);
assert.deepEqual(rows.filter(r => r.kind === 'add'), [
  { kind: 'add', after: 3, text: '+new' },
  { kind: 'add', after: 10, text: '+++ literal' },
  { kind: 'add', after: 11, text: '+' },
]);
assert.equal(rows.filter(r => r.kind === 'meta').length, 1);
assert.equal(diffRows('@@ -0,0 +1 @@\n+created\n')[1].after, 1);
assert.equal(diffRows('@@ -1 +0,0 @@\n-deleted\n')[1].before, 1);
console.log('Diff gutter checks passed: context, insertions, deletions, multiple hunks, literal prefixes and missing newline.');
