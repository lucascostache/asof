/* AsOf widget kit: dependency-free web components for receipts and ledgers. No network calls, no globals except `AsofWidgets`.
 *
 * Every widget is a PURE function from JSON to an HTML string (AsofWidgets.render(name, data, opts)); the custom elements only call it. Same data in, byte-identical HTML out:
 * nothing here reads the clock, a random number or the DOM. Data comes from the `data` attribute (JSON text) or the `.data` property.
 * Every chart has a text alternative (an aria-label summary and a data table); motion is never required, and the CSS turns it off for prefers-reduced-motion.
 * A widget made from synthetic data (`synthetic` attribute or `data.synthetic: true`) shows a small "synthetic" tag.
 */
(function (root) {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function esc(s) { return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function num(v, d) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d === undefined ? 2 : d) : '—'; }
  function int(v) { return (typeof v === 'number' && isFinite(v)) ? String(Math.round(v)) : '—'; }
  function pct(a, b) { return (typeof a === 'number' && typeof b === 'number' && b > 0) ? 100 * a / b : null; }
  /* Each value's share of `total` in whole percent, from the UNROUNDED values, rounded so the shares add up to 100 (largest remainder: round everything down, then give the missing points to the parts
     with the biggest fractional remainders, the earlier part first on a tie). The same rule as compute.percent_shares in Python, so a payload that arrives without shares gets the same numbers. */
  function shares(values, total) {
    var ok = values.every(function (v) { return typeof v === 'number' && isFinite(v); });
    if (!ok || typeof total !== 'number' || !isFinite(total) || total === 0) return values.map(function () { return null; });
    var raw = values.map(function (v) { return 100 * v / total; }), fl = raw.map(Math.floor), left = Math.round(raw.reduce(function (a, b) { return a + b; }, 0)) - fl.reduce(function (a, b) { return a + b; }, 0),
      order = raw.map(function (r, i) { return i; }).sort(function (a, b) { return (raw[b] - fl[b]) - (raw[a] - fl[a]) || a - b; });
    for (var k = 0; k < left; k++) fl[order[k]] += 1;
    return fl;
  }
  function short(h, n) { return h ? esc(String(h).slice(0, n || 12)) + '…' : '—'; }
  function ms(t) { var x = (typeof t === 'number') ? t : Date.parse(t); return isFinite(x) ? x : null; }
  function day(t) { var x = ms(t); return x === null ? '—' : new Date(x).toISOString().slice(0, 10); }
  function stamp(t) { var x = ms(t); return x === null ? '—' : new Date(x).toISOString().slice(0, 19).replace('T', ' ') + 'Z'; }
  function span(sec) {
    if (typeof sec !== 'number' || !isFinite(sec)) return '—';
    var a = Math.abs(sec);
    return a >= 86400 ? num(sec / 86400, 1) + ' d' : a >= 3600 ? num(sec / 3600, 1) + ' h' : a >= 60 ? num(sec / 60, 1) + ' min' : num(sec, 0) + ' s';
  }
  function median(a) { if (!a.length) return null; var s = a.slice().sort(function (x, y) { return x - y; }), m = Math.floor(s.length / 2); return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }

  function wrap(name, title, body, d, o) {
    var syn = (d && d.synthetic === true) || (o && o.synthetic);
    return '<div class="asof-w asof-' + name + ((o && o.compact) ? ' asof-compact' : '') + ((o && o.rounded) ? ' asof-rounded' : '') + '" role="group" aria-label="' + esc(title) + '"><div class="asof-hd"><span class="asof-ttl">' + esc(title) + '</span>' +
      (syn ? '<span class="asof-synth" title="Made from synthetic data">synthetic</span>' : '') + '</div>' + body + '</div>';
  }
  function row(k, v) { return '<div class="asof-row"><dt>' + esc(k) + '</dt><dd>' + v + '</dd></div>'; }
  function badge(label) { return '<span class="asof-badge asof-b-' + esc(String(label).toLowerCase()) + '">' + esc(label) + '</span>'; }
  function sub(t) { return '<h3 class="asof-sub">' + esc(t) + '</h3>'; }
  function tiles(a) { return '<div class="asof-tiles">' + a.map(function (t) { return '<div class="asof-tile' + (t[2] ? ' asof-tile-' + t[2] : '') + (t[3] ? ' asof-tile-' + t[3] : '') + '"><b>' + esc(t[0]) + '</b><span>' + esc(t[1]) + '</span></div>'; }).join('') + '</div>'; }
  function meter(label, p, cls, detail) {
    var w = p === null ? 0 : Math.max(0, Math.min(100, p));
    return '<div class="asof-meter"><div class="asof-mrow"><span>' + esc(label) + '</span><span>' + esc(detail) + '</span></div><div class="asof-track" role="img" aria-label="' + esc(label + ': ' + detail) + '"><i class="asof-fill' + (cls ? ' ' + cls : '') + '" style="width:' + w.toFixed(1) + '%"></i></div></div>';
  }
  function chips(items) { return '<p class="asof-idl">' + items.map(function (i) { return '<code class="asof-id">' + esc(i) + '</code>'; }).join('') + '</p>'; }
  function table(head, rows, caption) {
    return '<details class="asof-alt"><summary>' + esc(caption) + '</summary><table><thead><tr>' + head.map(function (h) { return '<th scope="col">' + esc(h) + '</th>'; }).join('') +
      '</tr></thead><tbody>' + rows.map(function (r) { return '<tr>' + r.map(function (c) { return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></details>';
  }
  function bad(name, why) { return '<div class="asof-w asof-error" role="alert">Cannot show ' + esc(name) + ': ' + esc(why) + '</div>'; }
  function list(items, cls) { return '<ul class="' + (cls || 'asof-ul') + '">' + items.map(function (l) { return '<li>' + esc(l) + '</li>'; }).join('') + '</ul>'; }

  /* ---------------------------------------------------------------- charts */
  var W = 640, H = 200, PL = 46, PR = 12, PT = 12, PB = 26;
  var XR = W - PR - 48;      /* the right edge of a line chart's plot. The 48 units to its right are kept empty for the end-value label, so the label can never sit on the line or the dot */
  function scale(lo, hi, a, b) { var s = hi - lo; return function (v) { return s === 0 ? (a + b) / 2 : a + (v - lo) * (b - a) / s; }; }
  function path(pts, X, Y) {
    var out = [], pen = false;
    pts.forEach(function (p) { if (p.y === null || p.x === null) { pen = false; return; } out.push((pen ? 'L' : 'M') + num(X(p.x), 1) + ' ' + num(Y(p.y), 1)); pen = true; });
    return out.join(' ');
  }
  function areaPath(pts, X, Y, yb) {
    var segs = [], cur = [];
    pts.forEach(function (p) { if (p.y === null || p.x === null) { if (cur.length) segs.push(cur); cur = []; } else cur.push(p); });
    if (cur.length) segs.push(cur);
    return segs.filter(function (s) { return s.length > 1; }).map(function (s) {
      return 'M' + num(X(s[0].x), 1) + ' ' + num(yb, 1) + s.map(function (p) { return ' L' + num(X(p.x), 1) + ' ' + num(Y(p.y), 1); }).join('') + ' L' + num(X(s[s.length - 1].x), 1) + ' ' + num(yb, 1) + ' Z';
    }).join(' ');
  }
  /* series: [{name, cls, area?, pts:[{x(ms), y(number|null)}]}]. Optional `band`: [{x, lo, hi}] shaded between two values. */
  function lines(series, o) {
    var xs = [], ys = [];
    series.forEach(function (s) { s.pts.forEach(function (p) { if (p.x !== null) xs.push(p.x); if (p.y !== null) ys.push(p.y); }); });
    if (!xs.length || !ys.length) return null;
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs), y0 = Math.min.apply(null, ys), y1 = Math.max.apply(null, ys);
    if (y0 === y1) { y0 -= 1; y1 += 1; }
    var X = scale(x0, x1, PL, XR), Y = scale(y0, y1, H - PB, PT), g = '';
    g += '<line class="asof-axis" x1="' + PL + '" y1="' + (H - PB) + '" x2="' + XR + '" y2="' + (H - PB) + '"/><line class="asof-axis" x1="' + PL + '" y1="' + PT + '" x2="' + PL + '" y2="' + (H - PB) + '"/>';
    if (y0 < 0 && y1 > 0) g += '<line class="asof-zero" x1="' + PL + '" y1="' + num(Y(0), 1) + '" x2="' + XR + '" y2="' + num(Y(0), 1) + '"/>';
    if (o && o.band && o.band.length > 1) {
      var up = o.band.map(function (b) { return num(X(b.x), 1) + ',' + num(Y(b.hi), 1); }), dn = o.band.slice().reverse().map(function (b) { return num(X(b.x), 1) + ',' + num(Y(b.lo), 1); });
      g += '<polygon class="asof-band" points="' + up.concat(dn).join(' ') + '"/>';
    }
    [0.25, 0.5, 0.75].forEach(function (f) { var y = PT + f * (H - PB - PT); g += '<line class="asof-grid" x1="' + PL + '" y1="' + num(y, 1) + '" x2="' + XR + '" y2="' + num(y, 1) + '"/>'; });
    series.forEach(function (s) { if (s.area) g += '<path class="asof-area" d="' + areaPath(s.pts, X, Y, H - PB) + '"/>'; });
    series.forEach(function (s) { g += '<path class="asof-line ' + esc(s.cls) + '" d="' + path(s.pts, X, Y) + '" fill="none"/>'; });
    var last = series[series.length - 1], lp = null;
    last.pts.forEach(function (p) { if (p.y !== null && p.x !== null) lp = p; });
    if (lp) { var lx = X(lp.x), ly = Y(lp.y); g += '<circle class="asof-end" cx="' + num(lx, 1) + '" cy="' + num(ly, 1) + '" r="4"/>' + (o && o.hideValues ? '' : '<text class="asof-endv" x="' + (XR + 10) + '" y="' + num(Math.min(Math.max(ly + 4, PT + 10), H - PB), 1) + '">' + num(lp.y) + '</text>'); }      /* in the empty strip right of the plot, level with the dot: nothing is ever drawn there */
    g += '<text class="asof-tick" x="' + PL + '" y="' + (H - 8) + '">' + esc(day(x0)) + '</text><text class="asof-tick" x="' + XR + '" y="' + (H - 8) + '" text-anchor="end">' + esc(day(x1)) + '</text>' +
      (o && o.hideValues ? '' : '<text class="asof-tick" x="' + (PL - 6) + '" y="' + (PT + 8) + '" text-anchor="end">' + num(y1) + '</text><text class="asof-tick" x="' + (PL - 6) + '" y="' + (H - PB) + '" text-anchor="end">' + num(y0) + '</text>');
    var defs = '<defs><linearGradient id="asof-ga" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6fbf8f" stop-opacity=".30"/><stop offset="1" stop-color="#6fbf8f" stop-opacity="0"/></linearGradient></defs>';
    return { svg: '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + esc(o && o.label || '') + '" focusable="false">' + defs + g + '</svg>', x0: x0, x1: x1, y0: y0, y1: y1 };
  }
  /* a compact bar strip: values[i] >= 0 drawn as bars, the tallest fills the height */
  function bars(vals, xs, label, hideValues) {
    var h = 70, mx = Math.max.apply(null, vals.concat([1e-12])), n = vals.length, bw = n ? (XR - PL) / n : 0, g = '<line class="asof-axis" x1="' + PL + '" y1="' + (h - 14) + '" x2="' + XR + '" y2="' + (h - 14) + '"/>';      /* the same plot width as the line chart above it, so the two line up */
    vals.forEach(function (v, i) { var bh = (h - 22) * v / mx; g += '<rect class="asof-gbar' + (v > 1e-9 ? ' asof-gbar-on' : '') + '" x="' + num(PL + i * bw + 0.5, 1) + '" y="' + num(h - 14 - bh, 1) + '" width="' + num(Math.max(bw - 1, 1), 1) + '" height="' + num(bh, 1) + '"/>'; });
    g += '<text class="asof-tick" x="' + PL + '" y="' + (h - 2) + '">' + esc(day(xs[0])) + '</text><text class="asof-tick" x="' + XR + '" y="' + (h - 2) + '" text-anchor="end">' + esc(day(xs[n - 1])) + '</text><text class="asof-tick" x="' + (PL - 6) + '" y="14" text-anchor="end">' + (hideValues ? '' : num(mx)) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + h + '" role="img" aria-label="' + esc(label) + '" focusable="false">' + g + '</svg>';
  }
  /* When rows become available later than their event, say by how much: it is why a point can use fewer rows than days have passed, and why counts by event day and by availability differ. Not in the shared view. */
  function lagNote(d, shared) {
    var l = d && d.lag_seconds;
    return (!shared && l && typeof l.median === 'number') ? ' Availability lags the event: median ' + esc(span(l.median)) + ', longest ' + esc(span(l.max)) + '; a row counts only from the moment it was available.' : '';
  }
  function legend(items) { return '<ul class="asof-legend">' + items.map(function (i) { return '<li><span class="asof-sw ' + esc(i[0]) + '"></span>' + esc(i[1]) + '</li>'; }).join('') + '</ul>'; }
  function pts(arr, kx, ky) { return (arr || []).map(function (p) { return { x: ms(p[kx]), y: (typeof p[ky] === 'number' && isFinite(p[ky])) ? p[ky] : null }; }); }

  /* ---------------------------------------------------------------- the widgets */
  var R = {};

  R.receipt = function (d, o) {
    if (!d || typeof d !== 'object' || !d.call_id) return bad('receipt', 'it needs a receipt object with a call_id');
    var b = d.blocked || {}, t = d.served_timing || {}, m = d.model, gated = d.kind === 'query' || d.kind === 'tool', ev = d.event_seqs || [], shared = d.view === 'shared', ids = (shared ? d.served_ids_hmac : d.served_ids) || [];
    var nb = (b.future || 0) + (b.untimestamped || 0) + (b.superseded || 0) + (b.outside_window || 0);
    var body = tiles([[int(d.served), 'served', 'ok']].concat(gated ? [[int(nb), 'blocked', nb > 0 ? 'hot' : '']] : [], t.declared !== undefined ? [[int(t.unknown), 'unknown timing', t.unknown > 0 ? 'warm' : '']] : [], [[int(ev.length), 'ledger events', '']]));
    if (!o.compact && gated && t.declared !== undefined && d.served > 0) body += meter('Served items with a declared time', pct(t.declared, d.served), '', int(t.declared) + ' of ' + int(d.served));
    if (o.compact) return wrap('receipt', 'Receipt', body, d, o);
    body += '<dl class="asof-dl">' + row('Call', badge(d.kind || 'call') + ' ' + esc(d.target || '')) + row('Session clock T', shared ? '<span class="asof-mut">withheld in the shared view</span>' : '<code>' + esc(stamp(d.as_of)) + '</code>') +
      (!shared && d.quality && d.quality.lag_seconds && typeof d.quality.lag_seconds.median === 'number' ? row('Availability lag', esc('median ' + span(d.quality.lag_seconds.median) + ', longest ' + span(d.quality.lag_seconds.max)) +
        ' <span class="asof-mut">from a row’s event to its availability, among the served rows. A row whose availability is after T is withheld, however early its event.</span>') : '') +
      row('Recorded at', '<code>' + esc(stamp(d.recorded_at)) + '</code> <span class="asof-mut">wall clock, set only by the ledger</span>') + row('Privacy', esc(d.privacy || 'full')) +
      row('Ledger', '<code>#' + int(d.ledger_seq) + '</code> <code title="' + esc(d.ledger_hash || '') + '">' + short(d.ledger_hash, 16) + '</code>') + (ev.length ? row('Events', '<code>' + esc(ev.slice(0, 10).join(', ')) + (ev.length > 10 ? ' … +' + (ev.length - 10) : '') + '</code>') : '') + '</dl>';
    if (gated) body += sub('Withheld by the time lock') + '<dl class="asof-dl asof-dl2">' + row('Dated after T', '<b>' + int(b.future || 0) + '</b>') + row('No timestamp', '<b>' + int(b.untimestamped || 0) + '</b>') + row('Superseded', '<b>' + int(b.superseded || 0) + '</b>') +
      row('Outside window', '<b>' + int(b.outside_window || 0) + '</b>') + '</dl><p class="asof-mut">Operator view: even a count tells an agent that something exists later, so do not show it to the agent.</p>';
    if (ids.length) body += sub('Served (' + ids.length + (shared ? ', keyed row ids' : '') + ')') + chips(ids.slice(0, 8).map(function (i) { return String(i).length > 18 ? String(i).slice(0, 16) + '…' : i; }).concat(ids.length > 8 ? ['+' + (ids.length - 8) + ' more'] : []));
    if (shared && d.row_ids === 'no_secret') body += '<p class="asof-mut">Row ids are not shown: no client secret is set, and unkeyed ids are never displayed in the shared view.</p>';
    if (m) {
      var gf = (m.gateway && m.gateway.flags) || [];
      body += sub('Model') + '<dl class="asof-dl">' + row('Model', esc(m.id || '') + ' ' + badge(String(m.label || 'unknown').toUpperCase())) + (m.provider ? row('Provider', esc(m.provider)) : '') + (m.version_reported ? row('Served version', '<code>' + esc(m.version_reported) + '</code>') : '') +
        row('Declared cutoff', esc(m.declared_cutoff ? day(m.declared_cutoff) : 'unknown') + (m.declared_by && m.declared_by !== 'unknown' ? ' <span class="asof-mut">declared by ' + esc(m.declared_by) + '</span>' : '')) +
        (m.declared_cutoff_after_as_of ? row('Cutoff vs T', '<span class="asof-neg">the declared cutoff is after the session clock: it may know later events</span>') : '') +
        row('Contamination', m.contamination_score === null || m.contamination_score === undefined ? '<span class="asof-mut">not measured</span>' : num(m.contamination_score)) + '</dl>';
      if (gf.length) body += list(gf.map(function (f) { return f.kind + ': ' + (f.rule || (f.reported_by_provider || []).concat(f.requested || []).join(', ') || 'flagged'); }), 'asof-warn');
    }
    if (d.quality && d.quality.flags) body += sub('Data quality of what was served') + (d.quality.flags.length ? chips(d.quality.flags) : '<p class="asof-mut">No flags on this query.</p>') +
      (d.quality.tied_versions ? '<p class="asof-mut">tied_versions: ' + int(d.quality.tied_versions) + ' served record(s) had several versions with identical availability. The others are counted under Superseded; the tie is settled by explicit version, then recorded_at, never by row order.</p>' : '');
    if (d.warnings && d.warnings.length) body += list(d.warnings, 'asof-warn');
    if (d.limits && d.limits.length) body += '<details class="asof-alt"><summary>What this receipt does not show</summary>' + list(d.limits) + '</details>';
    return wrap('receipt', 'Receipt', body, d, o);
  };

  R['clock-timeline'] = function (d, o) {
    var evs = d && d.events;
    if (!Array.isArray(evs)) return bad('clock timeline', 'it needs {events: [{seq, type, event_time, available_at, recorded_at}]}');
    var lanes = [['event_time', 'Event time', 'asof-l1'], ['available_at', 'Available at', 'asof-l2'], ['recorded_at', 'Recorded at', 'asof-l3']], g = '', rng = {};
    lanes.forEach(function (l) { var v = evs.map(function (e) { return ms(e[l[0]]); }).filter(function (x) { return x !== null; }); rng[l[0]] = v.length ? [Math.min.apply(null, v), Math.max.apply(null, v)] : null; });
    var shared = [rng.event_time, rng.available_at].filter(Boolean), s0 = shared.length ? Math.min.apply(null, shared.map(function (r) { return r[0]; })) : 0, s1 = shared.length ? Math.max.apply(null, shared.map(function (r) { return r[1]; })) : 1;
    var T = d.as_of ? ms(d.as_of) : null, LW = 98, HH = 150, late = 0, lags = [], types = {};
    evs.forEach(function (e) {
      types[e.type] = (types[e.type] || 0) + 1;
      var a = ms(e.event_time), v = ms(e.available_at); if (a !== null && v !== null) lags.push((v - a) / 1000);
      if (T !== null && v !== null && v > T) late += 1;
    });
    lanes.forEach(function (l, i) {
      var y = 34 + i * 38, r = rng[l[0]], a = l[0] === 'recorded_at' ? (r ? r[0] : 0) : s0, b = l[0] === 'recorded_at' ? (r ? r[1] : 1) : s1, X = scale(a, b, LW, W - PR);
      g += '<rect class="asof-lanebg" x="' + LW + '" y="' + (y - 15) + '" width="' + (W - PR - LW) + '" height="30"/><text class="asof-lane" x="6" y="' + (y + 4) + '">' + esc(l[1]) + '</text><line class="asof-axis" x1="' + (LW + 8) + '" y1="' + y + '" x2="' + (W - PR - 8) + '" y2="' + y + '"/>';
      evs.forEach(function (e) {
        var t = ms(e[l[0]]); if (t === null) return;
        var lt = T !== null && l[0] !== 'recorded_at' && t > T;
        g += '<circle class="' + l[2] + (lt ? ' asof-late' : '') + '" cx="' + num(X(t), 1) + '" cy="' + y + '" r="4.5"><title>#' + esc(e.seq) + ' ' + esc(e.type) + ' ' + esc(stamp(e[l[0]])) + '</title></circle>';
      });
    });
    if (T !== null && T >= s0 && T <= s1) { var tx = scale(s0, s1, LW, W - PR)(T); g += '<line class="asof-now" x1="' + num(tx, 1) + '" y1="22" x2="' + num(tx, 1) + '" y2="' + (HH - 22) + '"/><rect class="asof-tpill" x="' + num(tx - 11, 1) + '" y="2" width="22" height="16"/><text class="asof-ttext" x="' + num(tx, 1) + '" y="14" text-anchor="middle">T</text>'; }
    var scaled = rng.recorded_at && rng.event_time && (rng.recorded_at[0] - rng.event_time[1]) > 30 * 86400000;
    var summary = evs.length + ' events on three clocks' + (T !== null ? '; the session clock T is ' + stamp(d.as_of) : '') + '. Event times ' + (rng.event_time ? day(rng.event_time[0]) + ' to ' + day(rng.event_time[1]) : 'unknown') + '.';
    var spanDays = rng.event_time ? (rng.event_time[1] - rng.event_time[0]) / 86400000 : null, tk = Object.keys(types).sort();
    var rows = evs.slice(0, 60).map(function (e) { return [e.seq, e.type, stamp(e.event_time), stamp(e.available_at), stamp(e.recorded_at)]; });
    var head = tiles([[int(evs.length), 'events', ''], [spanDays === null ? '—' : num(spanDays, 0) + ' d', 'event span', ''], [span(median(lags)), 'median lag to knowable', ''], [int(late), 'knowable after T', late > 0 ? 'hot' : 'ok']]) +
      '<svg viewBox="0 0 ' + W + ' ' + HH + '" role="img" aria-label="' + esc(summary) + '" focusable="false">' + g + '</svg>', rest = legend([['asof-l1', 'when it happened'], ['asof-l2', 'when it became knowable'], ['asof-l3', 'when it was recorded']]) +
      '<p class="asof-mut">' + (scaled ? 'Recorded-at is wall-clock time, far from the data’s own dates, so it has its own scale. ' : '') + 'A red dot is knowable only after the session clock T.</p>' +
      sub('Events by type') + chips(tk.map(function (k) { return k + ' × ' + types[k]; })) +
      '<p class="asof-sr">' + esc(summary) + '</p>' + table(['#', 'type', 'event_time', 'available_at', 'recorded_at'], rows, 'Events as a table' + (evs.length > 60 ? ' (first 60 of ' + evs.length + ')' : '')),
      body = head + (o.compact ? '<p class="asof-sr">' + esc(summary) + '</p>' : rest);
    return wrap('clock-timeline', 'Three clocks', body, d, o);
  };

  var SEV = { error: ['Error', 'Errors'], warning: ['Warning', 'Warnings'], info: ['Info', 'Info'] };
  var MEANS = {
    available_before_event: 'Records that became available before the thing they describe happened. Honest clocks do not do this.', future_dated: 'Records dated after the moment the summary was made.',
    unusable_timestamps: 'Timestamps with no time zone or that cannot be read, so the records cannot be placed in time.', missing_available_at: 'Records with no availability time: they cannot be shown to be past, so they are withheld by default.',
    missing_event_time: 'Records with no event time: their place in the history is unknown.', late_availability: 'Records published much later than the usual lag after their event: a backfill, or history that was restated.',
    bulk_backfill: 'Many records, spanning a long history, that all became available in the same hour: the signature of a load stamped with the loading time.', restatements: 'A record whose value changed after it was first published.',
    duplicate_rows: 'Rows that repeat an earlier row exactly.', gaps: 'Steps between consecutive events far larger than the usual step: missing history.', no_record_identity: 'The source gave no key, so versions of one record cannot be told from different records.',
    too_few_points_for_gaps: 'Too few distinct times to measure gaps.', partial_bars_withheld: 'Bars still forming at T were left out, as they should be.', availability_moved_to_close: 'A bar’s availability was moved to its close: it was not knowable earlier.'
  };
  R['quality-panel'] = function (d, o) {
    if (!d || typeof d !== 'object') return bad('quality panel', 'it needs a data-quality summary');
    if (d.recorded === false || d.v === undefined && !d.issues && !d.flags) return wrap('quality-panel', 'Data quality', '<p class="asof-unknown">Not recorded: this session has no data-quality summary, so its sources are not reported as clean.</p>', d, o);
    var sev = d.by_severity || { error: 0, warning: 0, info: 0 }, issues = d.issues || [], srcs = d.sources || [], body = tiles([[int(sev.error), SEV.error[sev.error === 1 ? 0 : 1], sev.error ? 'hot' : 'ok'], [int(sev.warning), SEV.warning[sev.warning === 1 ? 0 : 1], sev.warning ? 'warm' : 'ok'],
      [int(sev.info), 'Info', ''], [int(d.n_records), 'records checked', '']]);
    if (o.compact) return wrap('quality-panel', 'Data quality', body, d, o);
    var shared = d.view === 'shared';
    if (shared) body += '<p class="asof-mut">Shared view: source names are aliased; lag statistics, row ids and custom thresholds are withheld.</p>';
    if (srcs.length) {
      body += sub('Coverage');
      srcs.forEach(function (s) {
        var n = s.n_records, ev = s.event_time || {}, av = s.available_at || {};
        body += '<p class="asof-src"><b>' + esc(s.name) + '</b> <span class="asof-mut">' + int(n) + ' records · ' + esc(s.kind || 'series') + (!shared && s.lag_seconds && s.lag_seconds.median !== null ? ' · median lag ' + span(s.lag_seconds.median) + ', max ' + span(s.lag_seconds.max) : '') + '</span></p>' +
          meter('Event time present', pct(ev.n_present, n), (ev.n_missing ? 'asof-fill-warn' : ''), int(ev.n_present) + ' of ' + int(n)) + meter('Availability time present', pct(av.n_present, n), (av.n_missing ? 'asof-fill-warn' : ''), int(av.n_present) + ' of ' + int(n));
      });
    }
    if (!issues.length) body += '<p>' + (d.flags && d.flags.length ? 'Flags: ' + esc(d.flags.join(', ')) : 'No issues flagged.') + ' <span class="asof-mut">Only as strong as what the checks can see: they cannot verify a timestamp.</span></p>';
    else {
      body += sub('Issues, most serious first') + '<table class="asof-tbl"><thead><tr><th scope="col">Issue</th><th scope="col">Severity</th><th scope="col">Records</th><th scope="col">Basis</th></tr></thead><tbody>' + issues.map(function (i) {
        var f = String(i.flag).split(': ').pop();
        return '<tr><td>' + esc(String(i.flag).replace(/_/g, ' ')) + (MEANS[f] ? '<div class="asof-mean">' + esc(MEANS[f]) + '</div>' : '') + (!shared && i.example_row_ids_hmac && i.example_row_ids_hmac.length ? '<div class="asof-mean">e.g. ' + i.example_row_ids_hmac.slice(0, 3).map(function (h) { return '<code>' + esc(String(h).slice(0, 10)) + '…</code>'; }).join(' ') + ' <span class="asof-mut">(keyed row ids)</span></div>' : '') +
          '</td><td><span class="asof-chip asof-s-' + esc(i.severity) + '">' + esc(i.severity) + '</span></td><td>' + int(i.n) + '</td><td>' + esc(i.basis) + '</td></tr>';
      }).join('') + '</tbody></table>';
      if (d.row_ids === 'no_secret') body += '<p class="asof-mut">Row ids are not shown: no client secret is set, and unkeyed hashes are never displayed.</p>';
    }
    var th = srcs.length && srcs[0].thresholds ? srcs[0].thresholds : null;
    if (th) body += '<details class="asof-alt"><summary>' + (d.thresholds_view === 'defaults' ? 'Thresholds (generic defaults)' : 'Thresholds used') + '</summary>' + list(Object.keys(th).sort().map(function (k) { return k.replace(/_/g, ' ') + ': ' + th[k]; })) + '</details>';
    body += '<p class="asof-mut">No single score. Counted issues are exact; heuristic ones rest on the stated thresholds and are never proof.</p>';
    return wrap('quality-panel', 'Data quality', body, d, o);
  };

  R['chain-status'] = function (d, o) {
    if (!d || typeof d !== 'object') return bad('chain status', 'it needs a verification result');
    var st = d.status || (d.ok === true ? 'intact' : d.ok === false ? 'broken' : 'unknown'), good = st === 'intact', head = d.head_hash || d.head, fb = d.first_bad_seq !== undefined ? d.first_bad_seq : d.broken_at;
    var line = good ? 'Chain intact · ' + int(d.n_events) + ' events' : st === 'broken' ? 'Chain broken at event ' + int(fb) : 'Chain ' + esc(st);
    var anchorTile = d.anchor === 'ok' ? ['ok', 'ok'] : d.anchor === 'failed' ? ['failed', 'hot'] : d.anchor === 'behind' ? ['behind', 'warm'] : [d.anchor || 'not checked', 'warm'];
    var top = '<p class="asof-big asof-c-' + (good ? 'ok' : st === 'broken' ? 'bad' : 'unk') + '" role="status">' + (good ? '● ' : st === 'broken' ? '○ ' : '◐ ') + esc(line) + '</p>' +
      tiles([[int(d.n_events), 'events', ''], [good ? 'yes' : st === 'broken' ? 'no' : '—', 'hash chain recomputes', good ? 'ok' : st === 'broken' ? 'hot' : ''], [anchorTile[0], 'tail anchor', anchorTile[1]]].concat(st === 'broken' ? [['#' + int(fb), 'first bad event', 'hot']] : [])),
      rest = '<dl class="asof-dl">' + row('Head hash', '<code title="' + esc(head || '') + '">' + short(head, 20) + '</code>') + (d.reason ? row('Why', '<span class="asof-bad">' + esc(d.reason) + '</span>') : '') + '</dl>' +
      (d.warnings && d.warnings.length ? list(d.warnings, 'asof-warn') : '') + sub('What this checked') +
      list(['Every event’s payload matches its recorded hash.', 'Each event links to the hash of the one before it, in order, with no gap.', 'The latest event matches the tail anchor kept beside the ledger (so a cut-off end is seen).']) +
      sub('What it does not show') + list(['That a source’s timestamps are true: only that they are unchanged since they were written.', 'That the anchor itself is trustworthy: copy the head to somewhere you trust and verify against that.']),
      body = top + (o.compact ? '' : rest);
    var html = wrap('chain-status', 'Ledger chain', body, d, o);
    return st === 'broken' ? html.replace('class="asof-w asof-chain-status"', 'class="asof-w asof-chain-status asof-broken"') : html;
  };

  R['rolling-sharpe'] = function (d, o) {
    if (!d || !Array.isArray(d.points)) return bad('rolling Sharpe', 'it needs {points: [{t, v}], window}');
    var shared = d.view === 'shared', p = pts(d.points, 't', 'v'), c = lines([{ name: 'Sharpe', cls: 'asof-l2', area: true, pts: p }], { label: 'Rolling Sharpe ratio over time, as of each point', hideValues: shared });
    var vals = d.points.filter(function (x) { return typeof x.v === 'number'; }).map(function (x) { return x.v; }), n = d.points.length, mean = vals.length ? vals.reduce(function (a, b) { return a + b; }, 0) / vals.length : null;
    var latest = vals.length ? vals[vals.length - 1] : null, mn = vals.length ? Math.min.apply(null, vals) : null, mx = vals.length ? Math.max.apply(null, vals) : null;
    var summary = 'Rolling Sharpe (window ' + int(d.window) + '), ' + vals.length + ' of ' + n + ' points defined' + (vals.length && !shared ? ', from ' + num(mn) + ' to ' + num(mx) : '') + '.';
    var head = tiles(shared ? [[int(n), 'points', ''], [int(vals.length), 'points defined', ''], [int(d.window), 'window (rows)', '']] : [[num(latest), 'latest', latest !== null && latest < 0 ? 'warm' : 'ok'], [num(mean), 'mean', ''], [num(mn), 'lowest', ''], [num(mx), 'highest', '']]) +
      (c ? c.svg.replace('aria-label="Rolling Sharpe ratio over time, as of each point"', 'aria-label="' + esc(summary) + '"') : '<p class="asof-unknown">No point has enough data yet.</p>'),
      rest = meter('Points with a defined value', pct(vals.length, n), '', vals.length + ' of ' + n) +
      '<p class="asof-mut">Window ' + int(d.window) + ' returns' + (d.periods_per_year ? ', annualised with ' + int(d.periods_per_year) + ' periods a year' + (d.calendar ? ' (' + esc(d.calendar) + ')' : '') : '') + '. Each point uses only rows with available_at ≤ that point’s time; a point with fewer than ' + int(d.window) + ' such rows is blank, never 0.' + lagNote(d, shared) + '</p><p class="asof-sr">' + esc(summary) + '</p>' + (shared ? '<p class="asof-mut">Shared view: the line is indexed 0–100 over this series, so its shape is shown but not its level; raw values and the table are withheld.</p>' :
      table(['time', 'Sharpe', 'rows used'], d.points.slice(0, 60).map(function (x) { return [stamp(x.t), num(x.v), int(x.n)]; }), 'Points as a table')),
      body = head + (o.compact ? '<p class="asof-sr">' + esc(summary) + '</p>' : rest);
    return wrap('rolling-sharpe', d.title || 'Rolling Sharpe, as of', body, d, o);
  };

  R['leak-overlay'] = function (d, o) {
    if (!d || !Array.isArray(d.locked) || !Array.isArray(d.unlocked)) return bad('leak overlay', 'it needs {locked: [{t, v}], unlocked: [{t, v}]}');
    var shared = d.view === 'shared', a = pts(d.locked, 't', 'v'), b = pts(d.unlocked, 't', 'v'), band = [], gaps = [], gx = [];
    d.locked.forEach(function (x, i) {
      var u = d.unlocked[i];
      if (u && ms(u.t) === ms(x.t) && typeof x.v === 'number' && typeof u.v === 'number') { band.push({ x: ms(x.t), lo: Math.min(x.v, u.v), hi: Math.max(x.v, u.v) }); gaps.push(Math.abs(u.v - x.v)); gx.push(ms(x.t)); }
    });
    var s = d.summary || {}, label = 'difference attributable to look-ahead (observed)', hit = gaps.filter(function (g) { return g > 1e-9; }).length;
    var unitp = shared ? '%' : '', summary = (d.metric || 'Metric') + ' computed locked (only data available at each time) and unlocked (everything). Largest gap ' + num(s.max_abs_gap, shared ? 1 : undefined) + unitp + (shared ? ' of the range' : '') + ', mean gap ' + num(s.mean_abs_gap, shared ? 1 : undefined) + unitp + ' over ' + int(s.n_compared) + ' points.';
    var c = lines([{ name: 'unlocked', cls: 'asof-l3', pts: b }, { name: 'locked', cls: 'asof-l2', pts: a }], { band: band, label: summary, hideValues: shared });
    var body = tiles([[num(s.max_abs_gap, shared ? 1 : undefined) + unitp, 'largest gap' + (shared ? ' (of range)' : ''), s.max_abs_gap > 0 ? 'hot' : 'ok'], [num(s.mean_abs_gap, shared ? 1 : undefined) + unitp, 'mean gap' + (shared ? ' (of range)' : ''), ''], [int(s.n_compared), 'points compared', ''], [gaps.length ? num(100 * hit / gaps.length, 0) + '%' : '—', 'points affected', hit ? 'warm' : 'ok']]) +
      (c ? c.svg : '<p class="asof-unknown">Nothing to compare yet.</p>');
    if (o.compact) return wrap('leak-overlay', d.title || 'Look-ahead leak overlay', body + '<p class="asof-gaplabel"><span class="asof-sw asof-bandsw"></span>' + esc(label) + '</p><p class="asof-sr">' + esc(summary) + '</p>', d, o);
    body += legend([['asof-l2', 'locked (as of)'], ['asof-l3', 'unlocked (look-ahead)']]) +
      '<p class="asof-gaplabel"><span class="asof-sw asof-bandsw"></span>' + esc(label) + '</p>';
    if (gaps.length) body += sub('Size of the gap at each point') + bars(gaps, gx, 'The absolute gap between the unlocked and locked values at each of ' + gaps.length + ' points' + (shared ? '.' : '; the largest is ' + num(Math.max.apply(null, gaps)) + '.'), shared);
    body += '<p class="asof-mut">Shaded area: ' + esc(label) + '. It is a measured difference between two runs, not proof of why. Locked uses only rows with available_at ≤ the point; unlocked uses every row whose event time is ≤ the point, as a backtest that ignores publication delay would.' + (d.periods_per_year ? ' Annualised with ' + int(d.periods_per_year) + ' periods a year' + (d.calendar ? ' (' + esc(d.calendar) + ')' : '') + '.' : '') + lagNote(d, shared) + '</p><p class="asof-sr">' + esc(summary) + '</p>' + (shared ? '<p class="asof-mut">Shared view: both lines are indexed 0–100 over the same range, so the gap between them is shown but not the levels; gaps are a % of that range. Raw values and the table are withheld.</p>' :
      table(['time', 'locked', 'unlocked', 'gap'], d.locked.slice(0, 60).map(function (x, i) { var u = d.unlocked[i] || {}; return [stamp(x.t), num(x.v), num(u.v), (typeof x.v === 'number' && typeof u.v === 'number') ? num(u.v - x.v) : '—']; }), 'Both series as a table'));
    return wrap('leak-overlay', d.title || 'Look-ahead leak overlay', body, d, o);
  };

  R['gap-breakdown'] = function (d, o) {
    if (!d || !Array.isArray(d.components)) return bad('gap breakdown', 'it needs {gap, components: [{id, name, value, label}]}');
    var sh = d.view === 'shared', orig = d;
    if (sh) {                                              /* the shared view carries each part's share of the gap and no values: draw it as a gap of 100 split into those shares */
      d = { title: orig.title, gap: 100, reconciles: orig.reconciles, view: 'shared', synthetic: orig.synthetic, annualisation: orig.annualisation, components: orig.components.map(function (c) { return { id: c.id, name: c.name, label: c.label, value: c.share, share: c.share }; }) };
    }
    var fmt = function (v) { return num(v) + (d.unit === 'pp' ? ' pp' : ''); };       /* pp = percentage points of annualised return: the parts of a return gap add up; a Sharpe ratio does not split that way */
    /* The percentages are computed ONCE, from the unrounded values, by `shares` (Python: compute.percent_shares, the same rule). Every view draws the same numbers: the internal view and the shared view
       both read each part's `share`, so they cannot differ, and the rounded shares add up to 100. Only a payload with no `share` (a raw attribution given straight to render) is worked out here. */
    var P = d.components.map(function (c) { return typeof c.share === 'number' ? c.share : null; });
    if (P.some(function (x) { return x === null; })) P = shares(d.components.map(function (c) { return typeof c.value === 'number' ? c.value : null; }), d.gap);
    var vals = P.map(function (x) { return x === null ? 0 : Math.abs(x); }), rows = [], list = '', un = null, unShare = null, explained = 0, neg = 0, pos = 0;
    P.forEach(function (x) { if (x !== null) { if (x > 0) pos = Math.max(pos, x); else neg = Math.max(neg, -x); } });
    /* one scale for every bar, the share. All parts on one side: bars start at the left edge and their length is the size. Parts on both sides: a zero line; a part that adds to the gap is drawn to its left, one that offsets it to its right. */
    var mixed = neg > 0 && pos > 0, span = mixed ? neg + pos : Math.max(neg, pos, 1e-9), zero = mixed ? 100 * pos / span : 0;
    d.components.forEach(function (c, i) {
      var v = typeof c.value === 'number' ? c.value : null, share = P[i], lab = String(c.label || 'UNKNOWN').toUpperCase(),
        w = share === null ? 0 : 100 * Math.abs(share) / span, left = mixed && share !== null && share > 0 ? zero - w : zero;
      if (c.id === 'unexplained') { un = v; unShare = share; } else if (share !== null) explained += share;
      list += '<li class="asof-gbr"><span class="asof-gbn">' + esc(c.name) + '</span><span class="asof-gbt" aria-hidden="true">' + (mixed ? '<i class="asof-gbz" style="left:' + zero.toFixed(1) + '%"></i>' : '') +
        '<i class="asof-gbf asof-bl-' + esc(c.id === 'leakage' ? 'leak' : lab.toLowerCase()) + '" style="left:' + left.toFixed(1) + '%;width:' + w.toFixed(1) + '%"></i></span><span class="asof-gbv">' + (v === null ? 'not computed' : sh ? '' : fmt(v)) + '</span><span class="asof-gbs">' + (share === null ? '' : num(share, 0) + '%') + '</span>' + badge(lab) + '</li>';
      rows.push(sh ? [c.name, share === null ? '—' : num(share, 0) + '%', lab] : [c.name, v === null ? 'not computed' : fmt(v), share === null ? '—' : num(share, 0) + '%', lab]);
    });
    var summary = (sh ? 'The gap split into ' : 'Gap of ' + fmt(d.gap) + ' split into ') + d.components.map(function (c) { return c.name + ' ' + (typeof c.value === 'number' ? (sh ? num(c.value, 0) + '%' : fmt(c.value)) : 'not computed'); }).join(', ') + '.';
    var hasCause = d.components.some(function (c) { return c.id === 'leakage' || c.id === 'data_issues'; }), rc = !sh && d.row_counts && typeof d.row_counts.both === 'number' ? d.row_counts : null;
    var hl = !sh && d.headline && typeof d.headline.sharpe_gap === 'number' ? d.headline : null, ann = d.annualisation && typeof d.annualisation.periods_per_year === 'number' ? d.annualisation : null;
    var largest = null;                                                                       /* the part with the biggest share of the gap (Unexplained counts too); the first one on a tie */
    d.components.forEach(function (c, i) { if (P[i] !== null && (largest === null || Math.abs(P[i]) > Math.abs(largest.share))) largest = { name: c.name, share: P[i] }; });
    var shareExp = P.some(function (x) { return x === null; }) ? null : explained;                  /* the shares of every part but Unexplained, added: 100 minus Unexplained's share */
    var tot = vals.reduce(function (a, b) { return a + b; }, 0), sx = 0, stack = '';
    d.components.forEach(function (c, i) {
      if (!(vals[i] > 0) || !(tot > 0)) return;
      var w = 640 * vals[i] / tot, lb = c.id === 'leakage' ? 'leak' : String(c.label || 'unknown').toLowerCase(), p = 100 * vals[i] / tot;
      stack += '<rect class="asof-bar asof-bl-' + esc(lb) + '" x="' + num(sx + 1, 1) + '" y="0" width="' + num(Math.max(w - 2, 1), 1) + '" height="14"><title>' + esc(c.name) + ' ' + num(p, 0) + '% of the gap</title></rect>';
      sx += w;
    });
    var shareBar = tot > 0 ? '<svg class="asof-stack" viewBox="0 0 640 14" role="img" aria-label="' + esc('Share of the gap by cause: ' + d.components.map(function (c, i) { return c.name + ' ' + num(100 * vals[i] / tot, 0) + '%'; }).join(', ') + '.') + '" focusable="false">' + stack + '</svg>' : '';
    var body = tiles([sh ? [int(d.components.length), 'parts', ''] : [fmt(d.gap), 'return gap (actual − expected)', typeof d.gap === 'number' && d.gap < 0 ? 'warm' : 'ok', 'wide']].concat(hl ? [[num(hl.sharpe_gap), '≈ Sharpe gap (headline only)', hl.sharpe_gap < 0 ? 'warm' : 'ok']] : []).concat([largest ? [largest.name, 'largest contributor, ' + num(largest.share, 0) + '% of the gap', 'name'] : ['—', 'largest contributor', ''], [sh ? (unShare === null ? '—' : num(unShare, 0) + '%') : fmt(un), sh ? 'unexplained (of the gap)' : 'unexplained', 'warm'], [shareExp === null ? '—' : num(shareExp, 0) + '%', 'attributed', 'ok']]).slice(0, o.compact ? 4 : 9)) + (o.compact ? shareBar + '<p class="asof-sr">' + esc(summary) + '</p>' : '') +
      (o.compact ? '' : '<ol class="asof-gb">' + list + '</ol>' + (hasCause ? '<p class="asof-def"><b>Look-ahead leakage</b> = rows used before their available_at. <b>Data issues</b> = wrong or revised values (restatements, survivorship, bad rows). A row is counted in one of the two, never both' +
        (rc ? ' (this run: ' + int(rc.leakage) + ' leakage, ' + int(rc.data_issues) + ' data-issue, ' + int(rc.both) + ' in both)' : '') + '.</p>' : '') +
      '<p class="asof-mut">' + (mixed ? 'Bar length is the part’s share of the gap: bars left of the line add to it, bars right of it offset it. ' : 'Bar length is the part’s share of the gap. ') + 'The parts add up to the gap exactly' + (d.reconciles === false ? ' (they do NOT here: see the table)' : '') + '; what cannot be attributed to a named cause is shown as Unexplained and labelled UNKNOWN, not explained away.' + (sh ? ' Shared view: only each part’s share of the gap is shown; the values and the total are withheld.' : '') + '</p>' +
      (hl ? '<p class="asof-mut">Sharpe gap = ' + esc(hl.method || 'the return gap expressed in Sharpe units') + '. A Sharpe ratio does not split into parts that add up, so the parts above are in return.</p>' : '') +
      (ann ? '<p class="asof-mut">Annualised with ' + int(ann.periods_per_year) + ' periods a year' + (ann.calendar ? '; ' + esc(ann.calendar) : '') + '.</p>' : '') + '<p class="asof-sr">' + esc(summary) + '</p>' +
      legend([['asof-sw-v', 'verified (arithmetic on recorded values)'], ['asof-sw-o', 'observed (a measurement, not proof)'], ['asof-sw-l', 'look-ahead leakage'], ['asof-sw-u', 'unknown']]) + table(sh ? ['part', 'share of gap', 'label'] : ['part', 'value', 'share of gap', 'label'], rows, 'Parts as a table'));
    return wrap('gap-breakdown', orig.title || 'Where the gap came from', body, orig, o);
  };

  R.series = function (d, o) {
    if (!d || !Array.isArray(d.series)) return bad('series', 'it needs {series: [{name, points: [{t, v}]}]}');
    var cls = ['asof-l2', 'asof-l3', 'asof-l1'], ss = d.series.map(function (s, i) { return { name: s.name, cls: cls[i % 3], pts: pts(s.points, 't', 'v') }; }), summary = esc(d.title || 'Series') + ': ' + d.series.map(function (s) { return s.name + ' (' + s.points.length + ' points)'; }).join(', ') + '.';
    var c = lines(ss, { label: summary.replace(/&[a-z#0-9]+;/g, '') });
    return wrap('series', d.title || 'Series', (c ? c.svg : '<p class="asof-unknown">No data.</p>') + legend(ss.map(function (s) { return [s.cls, s.name]; })) + '<p class="asof-sr">' + summary + '</p>' + (o.compact ? '' :
      table(['series', 'time', 'value'], [].concat.apply([], d.series.map(function (s) { return s.points.slice(0, 30).map(function (p) { return [s.name, stamp(p.t), num(p.v)]; }); })), 'Values as a table')), d, o);
  };

  /* ---------------------------------------------------------------- registry and elements */
  var TAGS = { 'asof-receipt': 'receipt', 'asof-clock-timeline': 'clock-timeline', 'asof-quality-panel': 'quality-panel', 'asof-chain-status': 'chain-status', 'asof-rolling-sharpe': 'rolling-sharpe',
    'asof-leak-overlay': 'leak-overlay', 'asof-gap-breakdown': 'gap-breakdown', 'asof-series': 'series' };

  function render(name, data, opts) {
    var fn = R[name];
    if (!fn) return bad(name, 'no such widget');
    if (data && data.__error) return bad(name, data.__error);
    try { return fn(data, opts || {}); } catch (e) { return bad(name, 'the data is not in the expected shape'); }
  }
  function register(name, fn) { if (R[name]) throw new Error('a widget named ' + name + ' already exists'); R[name] = fn; }

  /* Download as an image. Opt-in (the `downloadable` attribute) and browser-only: it runs on a click, so render() above stays pure and the HTML it makes is unchanged.
   * The picture is drawn locally (the widget's DOM with its computed styles, into an SVG image, onto a canvas) and saved by the browser; nothing is sent anywhere.
   * "Download image" gives the SHARED view (a widget that is already shared saves itself; an internal one saves its shared form, which is made server-side and travels in `shared-data`); the internal picture is a
   * separate button and always carries an INTERNAL banner. The "synthetic" tag is part of every picture. If a browser will not turn the drawing into a PNG, the SVG is saved instead. */
  function saveBlob(blob, file) {
    var a = document.createElement('a'), u = URL.createObjectURL(blob);
    a.href = u; a.download = file; a.style.display = 'none'; document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(u); }, 4000);
  }
  function backdrop(el) {
    for (var n = el.parentElement; n; n = n.parentElement) { var c = getComputedStyle(n).backgroundColor; if (c && c !== 'transparent' && !/,\s*0\)$/.test(c)) return c; }
    return el.closest('.asof-light') ? '#ffffff' : '#0d0e11';
  }
  function copyStyle(s, t) { var cs = getComputedStyle(s), i; for (i = 0; i < cs.length; i++) t.style.setProperty(cs[i], cs.getPropertyValue(cs[i])); }
  /* The picture of a widget, as SVG text (and its size). `o.data`: draw THAT data (the widget's shared view, made server-side) instead of what is on screen, in a hidden copy next to the widget.
   * `o.mark`: a banner across the top ("INTERNAL: NOT FOR SHARING"), used when the picture is of the internal view. */
  function imageSvg(host, name, o) {
    o = o || {};
    var shown = host.querySelector('.asof-w'), hidden = null, w = shown;
    if (!shown) return null;
    if (o.data) {                                                                            /* the shared view is drawn from its own data in a copy placed off screen, with the same page styles */
      hidden = document.createElement('div');
      hidden.setAttribute('aria-hidden', 'true');
      hidden.style.cssText = 'position:absolute;left:-10000px;top:0;width:' + Math.ceil(shown.getBoundingClientRect().width) + 'px';
      hidden.innerHTML = render(name, o.data, { synthetic: host.hasAttribute('synthetic'), compact: host.hasAttribute('compact'), rounded: host.hasAttribute('rounded') });
      host.parentNode.insertBefore(hidden, host.nextSibling);
      w = hidden.querySelector('.asof-w') || shown;
    }
    var box = w.getBoundingClientRect(), W = Math.ceil(box.width), H = Math.ceil(box.height), pad = 16, band = o.mark ? 28 : 0, TW = W + 2 * pad, TH = H + 2 * pad + band,
      clone = w.cloneNode(true), a = w.querySelectorAll('*'), b = clone.querySelectorAll('*'), i, holder = document.createElement('div'), tools = clone.querySelectorAll('.asof-download');
    copyStyle(w, clone);
    for (i = 0; i < a.length; i++) copyStyle(a[i], b[i]);
    for (i = 0; i < tools.length; i++) tools[i].parentNode.removeChild(tools[i]);           /* the button is not part of the picture */
    var pb = getComputedStyle(w, '::before');                                                /* a pseudo-element cannot be cloned: draw the accent line at the top as a real element */
    if (pb.content && pb.content !== 'none') {
      var bar = document.createElement('div');
      ['position', 'left', 'right', 'top', 'height', 'background-image', 'opacity'].forEach(function (k) { bar.style.setProperty(k, pb.getPropertyValue(k)); });
      clone.insertBefore(bar, clone.firstChild);
    }
    clone.style.setProperty('width', W + 'px'); clone.style.setProperty('margin', '0');
    var back = backdrop(w);
    if (hidden) hidden.parentNode.removeChild(hidden);                                         /* the copy has been read: take it away */
    holder.innerHTML = '<svg width="' + TW + '" height="' + TH + '" viewBox="0 0 ' + TW + ' ' + TH + '"><rect width="100%" height="100%" fill="' + esc(back) + '"></rect>' +
      (o.mark ? '<rect width="100%" height="' + band + '" fill="#d6a14b"></rect><text x="' + TW / 2 + '" y="18" text-anchor="middle" font-family="ui-monospace, Menlo, monospace" font-size="12" font-weight="700" letter-spacing="2" fill="#101215">' + esc(o.mark) + ': NOT FOR SHARING</text>' : '') +
      '<foreignObject x="' + pad + '" y="' + (pad + band) + '" width="' + W + '" height="' + H + '"></foreignObject></svg>';
    holder.querySelector('foreignObject').appendChild(clone);
    return { svg: new XMLSerializer().serializeToString(holder.firstChild), width: TW, height: TH, base: 'asof-' + (name || 'widget') + (o.mark ? '-' + String(o.mark).toLowerCase() : '') };
  }

  function download(host, name, o) {
    var p = imageSvg(host, name, o), scale = 2;
    if (!p) return false;
    function asSvg() { saveBlob(new Blob([p.svg], { type: 'image/svg+xml' }), p.base + '.svg'); }
    var img = new Image();
    img.onload = function () {
      try {
        var cv = document.createElement('canvas'), cx;
        cv.width = p.width * scale; cv.height = p.height * scale; cx = cv.getContext('2d'); cx.scale(scale, scale); cx.drawImage(img, 0, 0, p.width, p.height);
        cv.toBlob(function (bl) { if (bl) saveBlob(bl, p.base + '.png'); else asSvg(); }, 'image/png');
      } catch (e) { asSvg(); }
    };
    img.onerror = asSvg;
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(p.svg);
    return true;
  }

  function define(tag, name) {
    if (typeof customElements === 'undefined' || typeof HTMLElement === 'undefined' || customElements.get(tag)) return;
    customElements.define(tag, class extends HTMLElement {
      static get observedAttributes() { return ['data', 'synthetic', 'compact', 'downloadable', 'shared-data', 'rounded']; }
      connectedCallback() { this._draw(); }
      attributeChangedCallback() { if (this.isConnected) this._draw(); }
      get data() { if (this._d !== undefined) return this._d; var a = this.getAttribute('data'); if (!a) return null; try { return JSON.parse(a); } catch (e) { return { __error: 'the data attribute is not valid JSON' }; } }
      set data(v) { this._d = v; if (this.isConnected) this._draw(); }
      /* The widget's SHARED view data, made server-side (where the secret and the redaction rules live). Present on an internal widget that has a shared form. */
      get sharedData() { if (this._s !== undefined) return this._s; var a = this.getAttribute('shared-data'); if (!a) return null; try { return JSON.parse(a); } catch (e) { return null; } }
      set sharedData(v) { this._s = v; }
      _draw() {
        var self = this, hd, d = this.data, view = d && typeof d === 'object' ? d.view : undefined, internal = view !== 'shared' && view !== 'showcase', sd = internal ? this.sharedData : null;
        this.innerHTML = render(name, d, { synthetic: this.hasAttribute('synthetic'), compact: this.hasAttribute('compact'), rounded: this.hasAttribute('rounded') });
        hd = this.hasAttribute('downloadable') ? this.querySelector('.asof-hd') : null;
        if (!hd) return;
        function button(label, aria, run) {
          var btn = document.createElement('button');
          btn.type = 'button'; btn.className = 'asof-download'; btn.textContent = label; btn.setAttribute('aria-label', aria);
          btn.addEventListener('click', run);
          hd.appendChild(btn);
        }
        /* Download image: the SHARED view by default. A widget that is already a shared (or showcase) view saves itself; an internal one saves its shared form; an internal widget with no shared form (the chain,
           the three clocks) can only save itself, and says so in the picture. The internal picture is a second, labelled button and always carries the INTERNAL banner and "-internal" in its file name. */
        if (!internal) button('Download image', 'Download image: this widget as it is shown', function () { download(self, name, {}); });
        else if (sd) {
          button('Download image', 'Download image: the shared view of this widget', function () { download(self, name, { data: sd }); });
          button('Download internal image', 'Download internal image: the internal view, marked INTERNAL, not for sharing', function () { download(self, name, { mark: 'INTERNAL' }); });
        } else button('Download internal image', 'Download internal image: marked INTERNAL, not for sharing (this widget has no shared view)', function () { download(self, name, { mark: 'INTERNAL' }); });
      }
    });
  }
  Object.keys(TAGS).forEach(function (t) { define(t, TAGS[t]); });

  root.AsofWidgets = { render: render, register: register, tags: Object.keys(TAGS), names: function () { return Object.keys(R); }, define: define, download: download, imageSvg: imageSvg };
})(typeof window !== 'undefined' ? window : globalThis);
