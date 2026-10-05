// Unit test the generated HTML controller without browser or file-URL access.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const katex = require(path.join(__dirname, '../html_shinsoku/asset/katex/katex.min.js'));

class Element {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.listeners = {};
    this.classes = new Set();
    this.classList = {toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)};
  }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  dispatch(event) { this.listeners[event](); }
}

const sheets = fixture.titles.map(title => {
  const sheet = new Element();
  sheet.dataset.title = title;
  sheet.map = new Element();
  sheet.toggle = new Element('input');
  sheet.toggle.checked = true;
  sheet.querySelector = selector => selector === '.map_container' ? sheet.map : sheet.toggle;
  return sheet;
});
const formulas = fixture.formulas.map(latex => ({dataset: {latex}}));
const document = {
  body: new Element('body'),
  createElement: tag => new Element(tag),
  querySelectorAll: selector => selector === '[data-latex]' ? formulas : sheets,
};
const window = {katex: {render: (formula, element, options) => {
  const result = katex.renderToString(formula, {...options, throwOnError: true});
  assert(!result.includes('katex-error'));
  element.rendered = result;
}}};
vm.runInNewContext(fixture.script, {document, window});
assert(formulas.every(formula => formula.rendered));
const nav = document.body.children[0];
const [previous, label, next, printLabel] = nav.children;
const select = label.children[0];
const all = printLabel.children[0];
assert.equal(select.children.length, sheets.length);
assert.equal(sheets.filter(sheet => sheet.classes.has('active')).length, 1);
assert(previous.disabled);
next.dispatch('click');
assert(sheets[1].classes.has('active'));
assert(!previous.disabled);
select.selectedIndex = sheets.length - 1;
select.dispatch('change');
assert(sheets.at(-1).classes.has('active'));
assert(next.disabled);
previous.dispatch('click');
assert(sheets.at(-2).classes.has('active'));
sheets[0].toggle.checked = false;
sheets[0].toggle.dispatch('change');
assert.equal(sheets[0].map.dataset.mapVisible, '0');
assert.equal(sheets[1].map.dataset.mapVisible, '1');
all.checked = true;
all.dispatch('change');
assert.equal(document.body.dataset.printAll, '1');
all.checked = false;
all.dispatch('change');
assert.equal(document.body.dataset.printAll, '0');
console.log(`HTML controller: ${sheets.length} drawings, independent map toggles, print toggle: OK`);
console.log(`KaTeX: all ${formulas.length} formulas rendered without errors: OK`);
