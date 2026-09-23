/*
 * tests/js/vault_check.js — the device vault's storage rules.
 *
 * Saved analyses live in localStorage, which is ~5 MB per origin and throws when it is
 * full. That throw used to be swallowed, so the newest analysis silently never appeared.
 * The vault is now bounded (40 entries / 2 M characters) and says what it dropped.
 *
 * Runs the vault block exactly as shipped — sliced out of the template, not copied — with
 * a fake localStorage so the quota can be reached in a test. Node only, no dependencies:
 *
 *     node tests/js/vault_check.js
 */
'use strict';

const fs = require('fs');
const path = require('path');

const TEMPLATE = path.join(__dirname, '..', '..', 'templates', 'index.html');

let passed = 0;
const failures = [];
function check(name, condition, detail) {
  if (condition) { passed++; console.log('  ok   ' + name); }
  else { failures.push(name); console.log('  FAIL ' + name + (detail ? '\n       ' + detail : '')); }
}

// ── A localStorage that can run out, like the real one ───────────────────────
const store = new Map();
let quotaChars = Infinity;
global.localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => {
    const text = String(value);
    let others = 0;
    store.forEach((v, k) => { if (k !== key) others += v.length; });
    if (text.length + others > quotaChars) {
      const error = new Error('QuotaExceededError');
      error.name = 'QuotaExceededError';
      throw error;
    }
    store.set(key, text);
  },
  removeItem: key => store.delete(key),
};
function resetVault() { store.clear(); quotaChars = Infinity; }

// ── The shipped vault block, extracted verbatim ──────────────────────────────
const template = fs.readFileSync(TEMPLATE, 'utf8');
const start = template.indexOf('const VAULT_KEY');
const end = template.indexOf('function renderSavedCards');
if (start < 0 || end < 0 || end <= start) {
  console.error('Could not find the vault helpers in templates/index.html — did they move?');
  process.exit(2);
}
// eslint-disable-next-line no-eval
eval(template.slice(start, end) +
  '\n;globalThis.getSavedNotes = getSavedNotes; globalThis.putSavedNotes = putSavedNotes;' +
  '\nglobalThis.trimVault = trimVault; globalThis.vaultChars = vaultChars;' +
  '\nglobalThis.VAULT_MAX_ITEMS = VAULT_MAX_ITEMS; globalThis.VAULT_MAX_CHARS = VAULT_MAX_CHARS;');

const entry = (i, size = 50) => ({ id: i, title: 't' + i, markdown: 'x'.repeat(size) });

console.log('The vault stays inside its own budget:');
resetVault();
{
  const res = putSavedNotes(Array.from({ length: 60 }, (_, i) => entry(i)));
  check('60 saved analyses are trimmed to the item cap', getSavedNotes().length === VAULT_MAX_ITEMS,
        'kept ' + getSavedNotes().length);
  check('the newest survive, the oldest go', getSavedNotes()[0].id === 0 && getSavedNotes().at(-1).id === 39);
  check('the save reports what it dropped', res.ok === true && res.dropped === 20, JSON.stringify(res));
}
{
  resetVault();
  const big = Array.from({ length: 30 }, (_, i) => entry(i, 300000));   // over the char budget
  const res = putSavedNotes(big);
  const kept = getSavedNotes();
  check('a long vault is trimmed by characters, not just count', vaultChars(kept) <= VAULT_MAX_CHARS,
        vaultChars(kept) + ' > ' + VAULT_MAX_CHARS);
  check('and it still reports success', res.ok === true, JSON.stringify(res));
}

console.log('\nAn entry too big to store is refused, not half-stored:');
{
  resetVault();
  const res = putSavedNotes([entry(1, 4000000)]);
  check('refused', res.ok === false && res.reason === 'too-large', JSON.stringify(res));
  check('nothing was written', getSavedNotes().length === 0);
}
{
  resetVault();
  const first = putSavedNotes([entry(1)]);
  check('a normal entry is still accepted first', first.ok === true);
  const res = putSavedNotes([entry(1), entry(2, 4000000)]);
  check('an oversized oldest entry is dropped, the rest survive',
        res.ok === true && res.dropped === 1 && getSavedNotes().length === 1, JSON.stringify(res));
}
{
  resetVault();
  const first = putSavedNotes([entry(1)]);
  check('a normal entry is still accepted first', first.ok === true);
  // The entry the user is saving right now is the one that must not fit.
  const res = putSavedNotes([entry(9, 4000000), entry(1)]);
  check('an oversized newest entry is refused, the vault is left alone',
        res.ok === false && res.reason === 'too-large' && getSavedNotes().length === 1,
        JSON.stringify(res) + ' kept ' + getSavedNotes().length);
}

console.log('\nRuns out of room (the browser quota) — newest wins, oldest goes, and it says so:');
{
  resetVault();
  putSavedNotes([entry(1), entry(2)]);                 // 2 entries, both within our budget
  quotaChars = vaultChars(getSavedNotes()) + 10;       // the browser will not take another byte
  const res = putSavedNotes([entry(3), entry(1), entry(2)]);
  check('the save still succeeds', res.ok === true, JSON.stringify(res));
  check('it reports how much history it gave up', res.dropped >= 1, JSON.stringify(res));
  check('the analysis being saved is kept', getSavedNotes()[0].id === 3, JSON.stringify(getSavedNotes()));
  check('the vault ends inside the browser quota', vaultChars(getSavedNotes()) <= quotaChars);
}
{
  resetVault();
  putSavedNotes([entry(1, 150)]);
  quotaChars = vaultChars(getSavedNotes());            // not one more character fits
  const res = putSavedNotes([entry(2, 400)]);          // strictly larger than the quota
  check('when nothing fits, it gives up honestly', res.ok === false && res.reason === 'quota',
        JSON.stringify(res));
  check('and the vault is untouched', getSavedNotes().length === 1 && getSavedNotes()[0].id === 1,
        JSON.stringify(getSavedNotes()));
}
{
  resetVault();
  quotaChars = 2000;
  const res = putSavedNotes(Array.from({ length: 40 }, (_, i) => entry(i, 200)));
  check('under quota pressure it keeps what fits rather than failing outright',
        res.ok === true && getSavedNotes().length > 0 && getSavedNotes().length < 40,
        JSON.stringify(res) + ' kept ' + getSavedNotes().length);
}

console.log('\nReading a corrupt vault does not throw:');
{
  resetVault();
  localStorage.setItem('saved_pharma_notes', 'not json at all');
  check('unparseable vault reads as empty', Array.isArray(getSavedNotes()) && getSavedNotes().length === 0);
  resetVault();
  localStorage.setItem('saved_pharma_notes', '{"not":"an array"}');
  check('a non-array vault reads as empty', getSavedNotes().length === 0);
}

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length ? 1 : 0);
