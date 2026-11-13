# AsOf widgets

Dependency-free web components for receipts and ledgers: black and white glass, no network calls, no fonts or images loaded. One script (`asof-widgets.js`), one stylesheet (`asof-widgets.css`).

| element | reads | shows |
|---|---|---|
| `<asof-receipt>` | `Receipt.to_dict()` | what a call was served and blocked, the model label, the ledger hash |
| `<asof-clock-timeline>` | `{as_of, events:[{seq,type,event_time,available_at,recorded_at}]}` | the three clocks of every event, T drawn in |
| `<asof-quality-panel>` | a data-quality summary | issues by severity, each marked counted or heuristic; no single score |
| `<asof-chain-status>` | a verify result | intact or broken at which event, the head hash |
| `<asof-rolling-sharpe>` | `{window, points:[{t,v,n}]}` | the Sharpe ratio **as of** each point: only rows with `available_at` <= that time |
| `<asof-leak-overlay>` | `{locked, unlocked, summary}` | the same metric locked and unlocked, the gap shaded and labelled "difference attributable to look-ahead (observed)" |
| `<asof-gap-breakdown>` | a Verumark attribution | where the gap between expected and actual came from; Unexplained stays UNKNOWN |

```html
<link rel="stylesheet" href="asof-widgets.css"><script src="asof-widgets.js"></script>
<asof-chain-status data='{"ok":true,"n_events":25,"head_hash":"ab12..."}' synthetic></asof-chain-status>
<script>document.querySelector('asof-receipt').data = receiptObject</script>   <!-- or set .data -->
```

Bad or missing data draws an error box; it never throws. The `synthetic` attribute (or `"synthetic": true` in the data) adds a small "synthetic" tag: use it on every widget made from demo or fixture data.

## From Python

```python
w = session.show("receipt")                      # renders in Jupyter; .html() and .save("r.html") write it out
session.show("rolling-sharpe", rows=rows, window=20)
session.show("leak-overlay", rows=rows, window=20, synthetic=True)
```

`strathora report --html out.html --ledger ledger.db [--session ID] [--synthetic]` writes one self-contained file (chain, data quality, three clocks, receipts) with the script and stylesheet inlined. It is made from the ledger alone.

## Theme: square corners by default

Cards, tiles, pills, badges, bars, tracks, buttons and chart rectangles have square corners. The older rounded look is opt-in: `<asof-receipt rounded …>`, `element(…, rounded=True)`, `session.show(…, rounded=True)` or the class `asof-rounded` on a widget. The default is one rule at the end of `asof-widgets.css`; a test reads the computed corner radius of every element of every widget in a real browser (light theme and buttons included) and fails on any rounded one.

## Download as an image

```python
session.show("gap-breakdown", attribution=att, downloadable=True)     # or <asof-gap-breakdown downloadable ...> in HTML
```

Adds download buttons to the header. A click draws a widget into a PNG (2x, 16px margin); nothing is sent anywhere. It is off by default and is added in the browser, so `render()` and its snapshots do not change. The "synthetic" tag is part of every picture; the buttons are not.

* **Download image** saves the **shared view** (`asof-<widget>.png`). A widget that is already a shared (or showcase) view saves itself. An internal widget saves its shared form, which Python makes (the secret and the redaction rules live there, not in the browser) and embeds as `shared-data` (`element(..., downloadable=True, secret=...)`, or `show(..., downloadable=True)`); the page then carries the shared data too, and never the secret.
* **Download internal image** saves what is on screen with an amber **INTERNAL: NOT FOR SHARING** banner across the top, and `-internal` in the file name (`asof-<widget>-internal.png`). A widget with no shared form (the chain status and the three clocks are not redacted) has only this button.

Tested in Chromium by clicking the real buttons (file names, PNG size, the banner's pixels, what is in each picture); also tried by hand in WebKit and Firefox, where it downloads a PNG too. (Square corners, the default theme, look the same in all three; with `rounded`, WebKit's downloaded PNG still comes out square, that engine does not round corners in this kind of image.) If a browser refuses to turn the drawing into a PNG, the SVG is saved instead. Fonts are those of the machine that clicks.

## Privacy views of the data-quality panel

```python
session.show("quality-panel", secret="…")                 # internal (default): full detail; row ids keyed with the client secret
session.show("quality-panel", view="shared")               # source names aliased (Source A, B…); lag statistics, row ids and custom thresholds withheld
session.show("quality-panel", view="showcase")             # website: full content of demo data, generic default thresholds only
```

`strathora report --html out.html --external` draws the panel in the shared view (internal without the flag).

* **Row ids** are `HMAC-SHA256(client secret, plain hash)`, so the same row gives a different id for every client and nothing can be guessed without the secret. The secret is `secret=` or `$ASOF_CLIENT_SECRET` (at least 16 characters); it is never stored in the data, logged or put in an error. With no secret, ids are withheld, never shown as plain hashes. An unkeyed hash list is also stripped from anything a widget embeds.
* **Thresholds** are collapsed by default in every view; shared and showcase show the generic defaults, not a client's own tuning.
* **Every widget takes `view=`.** `shared` also changes the performance widgets and the receipt, in the data they carry (not only in how they are drawn): rolling Sharpe and the leak overlay carry values indexed 0–100 over the series (the shape and the gap stay, the level does not; the gap is a % of the range) and no table; the gap breakdown carries only each part's share of the gap, no values and no total; the receipt loses the session clock T and its served ids become keyed ids (the same id the data-quality panel gives that row, so one client can follow a row between them). `internal` is unchanged.
* **Not covered:** the ledger itself still holds the plain hashes (that is how a `hashes_only` ledger links an issue to its rows). An external report redacts the data-quality panel and the receipts; its chain and three-clocks widgets are not redacted, and a receipt still names its target source.

## Rolling Sharpe, as of

`rows` are `{event_time, available_at, ret}`. At each point in time the Sharpe ratio uses the last `window` returns whose `available_at` is at or before that time; a return published later is not used, however early its `event_time`. A point with fewer than `window` such returns is blank (unknown), never 0.
The leak overlay computes the same thing a second time ignoring `available_at` and shades the difference. That difference is **observed**: evidence of look-ahead between two runs, not a proof of cause.

## Plugins

```python
from asof import widgets

def doubled(rows, **kw):                                   # returns the JSON a component reads
    return {"series": [{"name": "2x", "points": [{"t": r["event_time"], "v": 2 * r["ret"]} for r in rows]}]}

widgets.register("doubled", doubled, {"tag": "asof-series", "title": "Doubled returns"})
session.show("doubled", rows=rows)
```

`register(name, compute_fn, render_spec)`: `render_spec["tag"]` must be one of the kit's components (a plugin supplies data, not markup); a name already in use is refused unless `replace=True`.
`rolling-sharpe` is registered this way, as the shipped example (`asof/widgets/__init__.py`, last line).

## Guarantees, and what they are tested by

* **Deterministic:** the same inputs give byte-identical HTML (sorted keys, no clock, no randomness). Snapshot tests render every widget from fixed synthetic fixtures under Node (`tests/test_widgets_js.py`; regenerate with `UPDATE_SNAPSHOTS=1` and read the diff).
* **Accessible:** every widget is a labelled group; every chart has an `aria-label` summary, a hidden text summary and a data table; nothing animates from script, and the one fade-in is off under `prefers-reduced-motion`.
* **Quiet:** the script has no `fetch`, no `XMLHttpRequest`, no URLs; the stylesheet loads nothing. Text from the data is escaped.

Not covered: a visual pixel comparison across browsers, and any claim that a chart is "correct" beyond the numbers it is given.
