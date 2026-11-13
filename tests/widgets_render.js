// Loads asof-widgets.js exactly as a browser would (with stand-ins for HTMLElement and customElements, nothing else) and renders a list of cases.
// usage: node widgets_render.js <asof-widgets.js> <cases.json>   ->   prints {name: html} as JSON. Also exercises the custom elements' own bad-data path.
const fs = require('fs');
const vm = require('vm');

const registry = {};
globalThis.HTMLElement = class {};
globalThis.customElements = { get: (t) => registry[t], define: (t, c) => { registry[t] = c; } };
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));

const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const c of cases) out[c.name] = globalThis.AsofWidgets.render(c.widget, c.data, c.opts || {});

// the elements: every tag is defined, and a bad `data` attribute shows an error box instead of throwing
const el = (tag, attr, flags) => {
  const o = new registry[tag]();
  o.getAttribute = (k) => (k === 'data' ? attr : null);
  o.hasAttribute = (k) => (flags || []).includes(k);
  o.isConnected = true;
  o._draw();
  return o.innerHTML;
};
out.__tags = Object.keys(registry).sort();
out.__bad_json = el('asof-receipt', '{not json');
out.__empty = el('asof-chain-status', null);
out.__synthetic_attr = el('asof-chain-status', '{"ok":true,"n_events":3,"head_hash":"abc"}', ['synthetic']);
out.__names = globalThis.AsofWidgets.names();
process.stdout.write(JSON.stringify(out));
