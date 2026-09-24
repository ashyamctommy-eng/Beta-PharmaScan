/*
 * tests/js/plain_text_check.js — formulas in the text the student copies out.
 *
 * "Copy" gives the Markdown source of an analysis, which is right for pasting somewhere
 * that renders it and wrong for an assignment: Word shows `\(C_{\text{acid}} = \frac{a}{b}\)`.
 * `plainTextFromMarkdown` is what the "Copy text" button hands the clipboard instead.
 *
 * Runs the shipped converter — sliced out of the template, not copied. Node only, no
 * dependencies:  node tests/js/plain_text_check.js
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

const template = fs.readFileSync(TEMPLATE, 'utf8');
const start = template.indexOf('const MATH_SLOT');
const end = template.indexOf('function semLabel');
if (start < 0 || end < 0 || end <= start) {
  console.error('Could not find the maths helpers in templates/index.html — did they move?');
  process.exit(2);
}
// eslint-disable-next-line no-eval
eval(template.slice(start, end) +
  '\n;globalThis.plainMath = plainMath; globalThis.plainTextFromMarkdown = plainTextFromMarkdown;');

console.log('The reported example reads as an assignment would write it:');
{
  const source = [
    'Calculate the volume of NaOH used: \\( V_{\\text{NaOH}} = V_{\\text{final}} - V_{\\text{initial}} \\).',
    '',
    'Calculate concentration',
    '\\[',
    'C_{\\text{acid}} = \\frac{C_{\\text{NaOH}} \\times V_{\\text{NaOH}}}{V_{\\text{acid}}}',
    '\\]',
    'where \\( C_{\\text{NaOH}} \\) is the known concentration of the titrant.',
  ].join('\n');
  const text = plainTextFromMarkdown(source);
  console.log('       → ' + JSON.stringify(text));
  check('no LaTeX delimiters left', !text.includes('\\(') && !text.includes('\\['));
  check('no commands left', !/\\[A-Za-z]+/.test(text), text);
  check('subscripts kept their name', text.includes('V_NaOH') && text.includes('V_final'));
  check('times became a multiplication sign', text.includes('×'), text);
  check('the fraction is explicit', text.includes('(C_NaOH × V_NaOH)/V_acid'), text);
  check('the prose is intact',
        text.includes('Calculate the volume of NaOH used') && text.includes('is the known concentration'));
  check('no empty braces left behind', !/[{}]/.test(text), text);
}

console.log('\nThe notations a pharmacy student actually meets:');
const cases = [
  ['\\( t_{1/2} = \\frac{0.693}{k} \\)', t => t.includes('t½') && t.includes('0.693/k'), 'half-life'],
  ['\\( C_1 V_1 = C_2 V_2 \\)', t => t.includes('C₁') && t.includes('C₂'), 'dilution'],
  ['\\( V_d \\)', t => t.includes('V_d'), 'volume of distribution'],
  ['\\( \\frac{1}{2} k t^2 \\)', t => t.includes('½') && t.includes('t²'), 'fraction + square'],
  ['\\( \\Delta t = 2 \\times t_{1/2} \\)', t => t.includes('Δ') && t.includes('t½'), 'delta'],
  ['\\( \\mu g \\) and \\( \\leq 5 \\)', t => t.includes('µg') && t.includes('≤'), 'micrograms, less-or-equal'],
  ['\\( \\sqrt{\\frac{V}{\\pi}} \\)', t => t.includes('√(V/π)'), 'nested square root'],
  ['\\( x^{10} + y_3 \\)', t => t.includes('x¹⁰') && t.includes('y₃'), 'multi-digit scripts'],
  ['\\( \\text{Clearance} = \\frac{Q}{C} \\)', t => t.includes('Clearance') && !t.includes('\\text'), 'text command'],
  ['\\( n = \\frac{m}{M} \\, \\text{mol} \\)', t => t.includes('n = m/M mol') && !t.includes('\\,'), 'thin space'],
];
for (const [source, verify, label] of cases) {
  const text = plainTextFromMarkdown(source);
  check(label + ' → ' + JSON.stringify(text), verify(text), text);
}

console.log('\nMarkdown furniture is flattened, not left raw:');
{
  const source = [
    '## Key Takeaways',
    '',
    '- **Bioavailability** (F) is the fraction reaching circulation.',
    '- See [the CDACC syllabus](https://example.test/syllabus).',
    '',
    '| Drug | Class |',
    '| --- | --- |',
    '| Amoxicillin | Beta-lactam |',
    '',
    '> Watch the renal dose.',
    '',
    '![Amoxicillin Structure](https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/amoxicillin/PNG)',
  ].join('\n');
  const text = plainTextFromMarkdown(source);
  console.log('       → ' + JSON.stringify(text));
  check('heading marks gone', !text.includes('#'));
  check('bold marks gone', !text.includes('**') && text.includes('Bioavailability'));
  check('bullets kept as bullets', text.includes('• Bioavailability'));
  check('link kept with its URL', text.includes('the CDACC syllabus (https://example.test/syllabus)'));
  check('structure image dropped', !text.includes('pubchem'));
  check('table rows survive as text', text.includes('Amoxicillin') && text.includes('Beta-lactam'));
  check('blockquote marker gone', !text.includes('>'));
}

console.log('\nIt must not invent or destroy content:');
{
  check('plain prose is unchanged', plainTextFromMarkdown('Aspirin is an NSAID.') === 'Aspirin is an NSAID.');
  const currency = plainTextFromMarkdown('A pack costs $5 in Nairobi.');
  check('currency is not treated as maths', currency.includes('$5'), currency);
  check('nothing is thrown away', plainTextFromMarkdown('# H\n\ntext').includes('text'));
}

console.log('\n' + passed + ' passed, ' + failures.length + ' failed');
process.exit(failures.length ? 1 : 0);
