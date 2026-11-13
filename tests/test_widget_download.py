"""The 'Download image' button: opt-in, added by the custom element in the browser on a click (so render() and its snapshots are untouched), the PNG is made locally.
The DEFAULT picture is the SHARED view; the internal picture is a second, labelled button and always carries an INTERNAL banner. The browser tests need Playwright and an installed browser; without them they are skipped, not passed."""
from __future__ import annotations

import base64
import html as html_lib
import json
import re
import struct
from pathlib import Path

import pytest

from asof.widgets import compute, element, js, page, redact, show

FIX = Path(__file__).parent / "fixtures" / "widgets"
SECRET = "download-test-secret-not-real"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def attribution():
    return compute.gap_breakdown(load("attribution"), synthetic=True)


def attr(el: str, name: str):
    m = re.search(rf' {name}="([^"]*)"', el)
    return json.loads(html_lib.unescape(m.group(1))) if m else None


# ---------------------------------------------------------------- what the element carries
def test_the_button_is_opt_in_and_a_plain_element_carries_nothing_extra():
    d = attribution()
    plain, dl = element("asof-gap-breakdown", d, synthetic=True), element("asof-gap-breakdown", d, synthetic=True, downloadable=True)
    assert "downloadable" not in plain and "shared-data" not in plain
    assert " downloadable" in dl and " shared-data=" in dl


def test_an_internal_widget_carries_its_shared_form_and_nothing_internal_rides_along_in_it():
    d = attribution()
    dl = element("asof-gap-breakdown", d, synthetic=True, downloadable=True)
    s = attr(dl, "shared-data")
    assert s == redact.shared_payload("asof-gap-breakdown", d) and s["view"] == "shared" and s["gap"] is None
    assert all("value" not in c for c in s["components"])                                              # percentages only
    raw = html_lib.unescape(re.search(r' shared-data="([^"]*)"', dl).group(1))
    for v in [d["gap"], d["headline"]["sharpe_gap"]] + [c["value"] for c in d["components"]]:
        assert str(v) not in raw, v                                                                    # none of the internal numbers is in the shared payload


def test_the_receipts_shared_form_has_keyed_ids_and_no_T_and_never_the_secret():
    r = {**load("receipt"), "synthetic": True}
    dl = element("asof-receipt", r, synthetic=True, downloadable=True, secret=SECRET)
    s = attr(dl, "shared-data")
    assert "served_ids" not in s and "as_of" not in s and s["row_ids"] == "hmac" and len(s["served_ids_hmac"]) == r["served"]
    assert SECRET not in dl and r["as_of"] in dl                                                       # T is in the internal data (that is the internal view), never in the shared form
    assert r["as_of"] not in html_lib.unescape(re.search(r' shared-data="([^"]*)"', dl).group(1))


def test_without_a_secret_the_shared_receipt_withholds_ids_rather_than_showing_plain_hashes(monkeypatch):
    monkeypatch.delenv("ASOF_CLIENT_SECRET", raising=False)
    s = attr(element("asof-receipt", load("receipt"), downloadable=True), "shared-data")
    assert s["row_ids"] == "no_secret" and "served_ids_hmac" not in s and "served_ids" not in s


def test_a_widget_that_is_already_shared_or_has_no_shared_form_gets_no_second_payload():
    shared = redact.widget("asof-gap-breakdown", attribution(), "shared")
    assert "shared-data" not in element("asof-gap-breakdown", shared, synthetic=True, downloadable=True)       # it is its own shared picture
    assert "shared-data" not in element("asof-chain-status", load("chain"), synthetic=True, downloadable=True)  # the chain has no shared form: its only picture is the labelled internal one
    q = element("asof-quality-panel", load("quality"), synthetic=True, downloadable=True)
    assert attr(q, "shared-data")["view"] == "shared"                                                  # the quality panel has one (aliased sources, no ids)


def test_show_passes_the_flag_and_the_shared_form_through_and_never_into_the_data():
    w = show(None, "gap-breakdown", attribution=load("attribution"), synthetic=True, downloadable=True)
    assert w.downloadable is True and w.shared_data["view"] == "shared" and "shared-data" in w.html(inline=False)
    assert "downloadable" not in json.dumps(w.data) and "shared_data" not in json.dumps(w.data)
    off = show(None, "gap-breakdown", attribution=load("attribution"), synthetic=True)
    assert off.downloadable is False and off.shared_data is None
    assert show(None, "gap-breakdown", attribution=load("attribution"), synthetic=True, view="shared", downloadable=True).shared_data is None


def test_the_script_keeps_its_promises_and_the_buttons_are_not_in_what_render_makes():
    src = js()
    assert "asof-download" in src and "createObjectURL" in src and "fetch(" not in src and "http://" not in src and "https://" not in src
    assert "Download image" in src and "Download internal image" in src and "NOT FOR SHARING" in src
    assert "Download image" not in element("asof-chain-status", {"ok": True, "n_events": 1, "head_hash": "ab"})       # added in the browser only


def test_the_buttons_class_does_not_collide_with_the_receipts_definition_lists():
    css = (Path(__file__).parents[1] / "asof" / "widgets" / "asof-widgets.css").read_text()
    assert ".asof-download" in css and ".asof-dl:hover" not in css and ".asof-dl { font" not in css


# ---------------------------------------------------------------- in a real browser
def _png_size(b: bytes) -> tuple[int, int]:
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", b[16:24])


PIXEL = """async (b64) => { const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const x = c.getContext('2d'); x.drawImage(img, 0, 0);
  return Array.from(x.getImageData(8, 8, 1, 1).data); }"""


@pytest.fixture(scope="module")
def pw():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                                          # noqa: BLE001  (no browser installed)
            pytest.skip(f"no Chromium for Playwright: {type(e).__name__}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def doc(pw, tmp_path_factory):
    d = attribution()
    shared = redact.widget("asof-gap-breakdown", d, "shared", SECRET)
    receipt = {**load("receipt"), "synthetic": True}
    body = (element("asof-gap-breakdown", d, synthetic=True, downloadable=True, secret=SECRET) + element("asof-gap-breakdown", shared, synthetic=True, downloadable=True)
            + element("asof-chain-status", load("chain"), synthetic=True, downloadable=True) + element("asof-receipt", receipt, synthetic=True, downloadable=True, secret=SECRET)
            + element("asof-gap-breakdown", d, synthetic=True))
    f = tmp_path_factory.mktemp("dl") / "p.html"
    f.write_text(page(body, title="dl"))
    pg = pw.new_page(viewport={"width": 820, "height": 900}, reduced_motion="reduce", accept_downloads=True)
    errs: list[str] = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f.as_uri())
    pg.wait_for_timeout(300)
    yield pg, errs, tmp_path_factory.mktemp("out")
    pg.close()


def test_each_kind_of_widget_gets_the_right_buttons(doc):
    pg, errs, _ = doc
    labels = pg.evaluate("[...document.querySelectorAll('[downloadable]')].map(e => [...e.querySelectorAll('button.asof-download')].map(b => b.textContent))")
    assert labels == [["Download image", "Download internal image"], ["Download image"], ["Download internal image"], ["Download image", "Download internal image"]]
    assert pg.locator("asof-gap-breakdown:not([downloadable]) button").count() == 0                                # opt-in: the fifth widget has no button
    assert not errs


def _download(pg, host_index: int, label: str, out: Path):
    with pg.expect_download(timeout=15000) as dl:
        pg.locator("[downloadable]").nth(host_index).locator("button.asof-download", has_text=re.compile(f"^{label}$")).click()
    p = out / f"{host_index}-{label.replace(' ', '_')}.png"
    dl.value.save_as(str(p))
    return dl.value.suggested_filename, p.read_bytes()


def test_download_image_is_the_shared_view_with_no_banner_and_the_internal_one_has_the_banner(doc):
    pg, _, out = doc
    n_before = pg.locator(".asof-w").count()
    name, shared_png = _download(pg, 0, "Download image", out)
    iname, internal_png = _download(pg, 0, "Download internal image", out)
    assert name == "asof-gap-breakdown.png" and iname == "asof-gap-breakdown-internal.png"
    shared_corner = pg.evaluate(PIXEL, base64.b64encode(shared_png).decode())
    internal_corner = pg.evaluate(PIXEL, base64.b64encode(internal_png).decode())
    assert shared_corner[0] < 40 and shared_corner[1] < 40 and shared_corner[2] < 40                         # the dark backdrop: no banner
    assert internal_corner[0] > 190 and 120 < internal_corner[1] < 190 and internal_corner[2] < 110           # the amber INTERNAL banner
    (sw, sh), (iw, ih) = _png_size(shared_png), _png_size(internal_png)
    h = pg.evaluate("[...document.querySelectorAll('[downloadable] .asof-w')].map(w => Math.ceil(w.getBoundingClientRect().height))")      # on screen: [internal gap, shared gap, chain, receipt]
    assert h[0] != h[1]                                                                                        # the two views really are different sizes, so size tells them apart
    assert abs(sh - 2 * (h[1] + 32)) <= 8 and abs(sh - 2 * (h[0] + 32)) > 40                                  # "Download image" is the size of the SHARED widget (made from the same data), not of the internal one
    assert abs(ih - 2 * (h[0] + 32 + 28)) <= 8 and sw == iw                                                    # the internal picture is the internal widget plus the 28px banner, at 2x
    assert pg.locator(".asof-w").count() == n_before                                                           # the hidden copy used for the shared picture is gone again


def test_what_is_in_each_picture(doc):
    pg, _, _ = doc
    host = "document.querySelectorAll('[downloadable]')"
    shared = pg.evaluate(f"() => {{ const e = {host}[0]; return AsofWidgets.imageSvg(e, 'gap-breakdown', {{ data: e.sharedData }}).svg; }}")
    internal = pg.evaluate(f"() => AsofWidgets.imageSvg({host}[0], 'gap-breakdown', {{ mark: 'INTERNAL' }}).svg")
    d = attribution()
    assert "INTERNAL" not in shared and "largest contributor" in shared and "of the gap" in shared
    said = re.sub(r' style="[^"]*"', "", shared)                                                               # everything in the picture except the copied layout measurements (pixel sizes such as "25.5938px" are not data)
    for v in [f"{abs(d['gap']):.2f}", f"{abs(d['headline']['sharpe_gap']):.2f}"] + [f"{abs(c['value']):.2f}" for c in d["components"]]:
        assert v not in said, v                                                                           # no value, no total, no Sharpe gap in the default picture
    assert "INTERNAL: NOT FOR SHARING" in internal and f"{d['gap']:.2f} pp" in internal                         # the internal picture says what it is and keeps its numbers
    shown = lambda svg: [int(x) for x in re.findall(r'class="asof-gbs"[^>]*>(-?\d+)%<', svg)]                    # noqa: E731
    assert shown(shared) == shown(internal) and sum(shown(shared)) == 100                                      # the same percentages in both


def test_the_shared_receipt_picture_hides_T_and_the_plain_ids_the_internal_one_shows_them(doc):
    pg, _, _ = doc
    shared = pg.evaluate("() => { const e = document.querySelectorAll('[downloadable]')[3]; return AsofWidgets.imageSvg(e, 'receipt', { data: e.sharedData }).svg; }")
    internal = pg.evaluate("() => AsofWidgets.imageSvg(document.querySelectorAll('[downloadable]')[3], 'receipt', { mark: 'INTERNAL' }).svg")
    T = load("receipt")["as_of"][:10]
    assert T not in shared and "withheld in the shared view" in shared and "keyed row ids" in shared
    assert T in internal and "INTERNAL: NOT FOR SHARING" in internal
    assert SECRET not in shared and SECRET not in internal


def test_a_widget_with_no_shared_form_can_only_save_the_labelled_internal_picture(doc):
    pg, _, out = doc
    name, png = _download(pg, 2, "Download internal image", out)
    assert name == "asof-chain-status-internal.png" and _png_size(png)[0] > 0
    corner = pg.evaluate(PIXEL, base64.b64encode(png).decode())
    assert corner[0] > 190 and corner[2] < 110                                                                 # amber banner
    already = _download(pg, 1, "Download image", out)
    assert already[0] == "asof-gap-breakdown.png"                                                              # an already shared widget saves itself, no banner
    assert pg.evaluate(PIXEL, base64.b64encode(already[1]).decode())[0] < 40
