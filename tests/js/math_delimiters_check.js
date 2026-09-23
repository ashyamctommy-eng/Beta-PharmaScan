/*
 * tests/js/math_delimiters_check.js — guards the LaTeX contract in templates/index.html.
 *
 * Why this exists: the model answers with LaTeX (`\( V = 3 \)`, `\[ n = \frac{m}{M} \]`).
 * Markdown reads a backslash before punctuation as an escape, so the delimiters reached the
 * screen as bare brackets and the underscores of `C_{\text{acid}}` became emphasis. The fix
 * is that every formula is lifted out of the text *before* marked sees it.
 *
 * This script runs the helper block exactly as shipped (it is sliced out of the template,
 * not copied) and asserts the contract without needing marked, DOMPurify, KaTeX or a
 * browser. Node only — no dependencies:
 *
 *     node tests/js/math_delimiters_check.js
 *
 * The typesetting itself (KaTeX) is browser-side and is not covered here.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const TEMPLATE = path.join(__dirname, '..', '..', 'templates', 'index.html');

let passed = 0;
const failures = [];
function check(name, condition, detail) {
  if (condition) {
    passed++;
    console.log('  ok   ' + name);
  } else {
    failures.push(name);
    console.log('  FAIL ' + name + (detail ? '\n       ' + detail : ''));
  }
}

// ── The shipped helpers, extracted verbatim ──────────────────────────────────
const template = fs.readFileSync(TEMPLATE, 'utf8');
const start = template.indexOf('const MATH_SLOT');
const end = template.indexOf('function semLabel');
if (start < 0 || end < 0 || end <= start) {
  console.error('Could not find the maths helpers in templates/index.html — did they move?');
  process.exit(2);
}
// Strictmode eval gets its own scope, so the helpers are re-exported explicitly.
// eslint-disable-next-line no-eval
eval(template.slice(start, end) +
  '\n;globalThis.protectMath = protectMath; globalThis.restoreMath = restoreMath;' +
  '\nglobalThis.renderMarkdownInto = renderMarkdownInto; globalThis.typesetMath = typesetMath;');

// ── Samples: the delimiters the model actually writes ────────────────────────
const SAMPLES = {
  'the reported example': [
    'Calculate the volume of NaOH used: \\( V_{\\text{NaOH}} = V_{\\text{final}} - V_{\\text{initial}} \\).\n\n' +
    'Calculate concentration\n\\[\nC_{\\text{acid}} = \\frac{C_{\\text{NaOH}} \\times V_{\\text{NaOH}}}{V_{\\text{acid}}}\n\\]\n' +
    'where \\( C_{\\text{NaOH}} \\) is the known concentration of the titrant.',
    3,
  ],
  'display with backslash brackets': ['Half-life:\n\\[ t_{1/2} = \\frac{0.693}{k} \\]', 1],
  'dollar display': ['Rate $$ k = \\frac{0.693}{t_{1/2}} $$ here', 1],
  'dollar inline with LaTeX': ['Clearance $CL = \\frac{Q \\cdot C_a}{C_v}$ per hour', 1],
  'environment block': ['\\begin{align} n &= \\frac{m}{M} \\end{align}', 1],
};

console.log('LaTeX is removed from the text Markdown parses:');
Object.entries(SAMPLES).forEach(([name, [source, expected]]) => {
  const { text, store } = protectMath(source);
  // No delimiter or backslash command may reach marked.
  const leak = text.match(/\\[()[\]]|\$\$/);
  check(name + ' — nothing for Markdown to escape', !leak, 'leaked: ' + JSON.stringify(leak && leak[0]));
  check(name + ' — ' + expected + ' formula(s) parked', store.length === expected, 'got ' + store.length);
  // Newlines inside a formula must be gone: with marked's breaks:true they would become <br>.
  check(name + ' — no LaTeX command reaches Markdown', !/\\[a-zA-Z]/.test(text), JSON.stringify(text));
});

console.log('\nFormulas come back as the canonical delimiters KaTeX auto-render knows:');
{
  const { text, store } = protectMath('inline \\( a \\) and display \\[ b \\]');
  const html = restoreMath(text, store);
  check('inline restored as \\( … \\)', html.includes('\\( a \\)'), html);
  check('display restored as $$ … $$', html.includes('$$ b $$'), html);
}

console.log('\nRound-trip is lossless, and the HTML is escaped before it is re-inserted:');
{
  const source = 'n = \\( \\frac{m}{M} \\) and \\( x < y \\) and \\( a & b \\)';
  const { text, store } = protectMath(source);
  const html = restoreMath(text, store);
  check('backslashes and braces intact', html.includes('\\frac{m}{M}'));
  check('< and & are escaped', html.includes('&lt;') && html.includes('&amp;') && !html.includes('< y'));
  check('no placeholder left behind', !html.includes('PHARMASCANMATH'));
}

console.log('\nMoney is not mistaken for mathematics:');
{
  const money = 'A pack costs $5 and another $10 in Nairobi.';
  const { text, store } = protectMath(money);
  check('dollar amounts untouched', text === money && store.length === 0, text);
  const prose = protectMath('Plain prose with (parentheses) and [brackets].');
  check('ordinary brackets untouched', prose.text.startsWith('Plain prose with (parentheses)'), prose.text);
}

console.log('\nUnterminated LaTeX is left visible rather than swallowed:');
{
  const { text } = protectMath('Broken: \\( \\frac{1}{ and text after.');
  check('unclosed delimiter does not delete the text',
    text.includes('and text after.') && text.includes('\\frac'), text);
}

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length ? 1 : 0);
