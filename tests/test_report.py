"""The standalone overview report: bars, cards and the whole page."""

from __future__ import annotations

import base64
import re

import pandas as pd
import pytest

from flatscorer import report
from flatscorer.scoring import SCORE_SCALE_MAX, Anchors, score_breakdown, score_colour

WEIGHTS = {"supermarket": 0.3, "bakery": 0.1, "pharmacy": 0.15, "gym": 0.15,
           "transit": 0.33, "green": 0.05, "noise": 0.05, "rent": 0.25}


def anchors(**overrides):
    base = {
        "saturation": {"supermarket": 2.0, "bakery": 2.0, "pharmacy": 1.0,
                        "gym": 1.0, "transit": 4.0, "green": 30.0},
        "rent_budget_eur": 2500.0,
        "commute_cap_min": 45.0,
        "noise_cap_m": 200.0,
        "destinations": {"Work": {"weight": 0.15}},
    }
    base.update(overrides)
    return Anchors(**base)


def metrics(**overrides):
    base = {"supermarket_count": 8, "bakery_count": 6, "pharmacy_count": 4, "gym_count": 3,
             "transit_count": 14, "green_score": 35.0, "noise_distance_m": 200.0,
             "rent_eur": 1250.0, "destinations_min": {"Work": 9.0}}
    base.update(overrides)
    return base


def breakdown_for(**overrides):
    return score_breakdown(metrics(**overrides), WEIGHTS, anchors())


# The score `breakdown_for()`'s own weights/metrics/anchors produce - computed
# once here so `sample_frame`'s default score and the deck it renders can never
# disagree, which is the point of the tests that compare them.
SAMPLE_SCORE = sum(t["contribution"] for t in breakdown_for().values())


def test_bar_tracks_sum_to_the_full_lane():
    """Track width is the term's share of the score, so the tracks always fill
    the lane exactly - that is what makes a short full bar and a long empty one
    read as different things."""
    bd = breakdown_for()
    order = report.term_order({"Flat A": bd})
    rows = report.bar_rows(bd, order)
    assert sum(r["track_pct"] for r in rows) == pytest.approx(100.0)


def test_bar_fills_sum_to_the_score():
    bd = breakdown_for()
    order = report.term_order({"Flat A": bd})
    rows = report.bar_rows(bd, order)
    score = sum(term["contribution"] for term in bd.values())
    earned = sum(r["fill_pct"] for r in rows) / 100.0 * SCORE_SCALE_MAX
    assert earned == pytest.approx(score)


def test_a_terms_fill_never_exceeds_its_own_track():
    bd = breakdown_for()
    order = report.term_order({"Flat A": bd})
    for row in report.bar_rows(bd, order):
        assert row["fill_pct"] <= row["track_pct"] + 1e-9


def test_terms_are_ordered_by_share_descending():
    bd = breakdown_for()
    order = report.term_order({"Flat A": bd})
    shares = [bd[key]["share"] for key in order]
    assert shares == sorted(shares, reverse=True)
    # transit carries the largest weight in WEIGHTS, so it leads.
    assert order[0] == "transit"


def test_every_card_uses_one_order_even_when_flats_differ():
    """The order is computed once and applied to every card, so the eye can
    compare straight down the column. Shares don't vary per candidate, but ties
    must not be resolved differently card to card either."""
    rich = breakdown_for()
    poor = breakdown_for(supermarket_count=0, transit_count=0, gym_count=0)
    order = report.term_order({"Rich": rich, "Poor": poor})
    assert [r["key"] for r in report.bar_rows(rich, order)] == order
    assert [r["key"] for r in report.bar_rows(poor, order)] == order


def test_a_destination_term_is_labelled_by_its_destination_name():
    assert report.term_label("dest_Work") == "→ Work"
    assert report.term_label("supermarket") == "Supermarkets"
    assert report.term_label("noise") == "Quiet"


def collect_log():
    lines = []
    return lines, lines.append


PNG_BYTES = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    b"IQAAAABJRU5ErkJggg=="
)


def test_an_http_image_is_used_as_is_and_escaped():
    lines, log = collect_log()
    src = report.image_src('https://example.com/a.jpg?x="1"', "Flat A", log)
    assert src.startswith("https://example.com/a.jpg")
    assert '"' not in src
    assert lines == []


def test_a_local_file_is_embedded_as_a_data_uri_with_its_mime_type(tmp_path):
    photo = tmp_path / "flat.png"
    photo.write_bytes(PNG_BYTES)
    lines, log = collect_log()
    src = report.image_src(str(photo), "Flat A", log)
    assert src.startswith("data:image/png;base64,")
    assert base64.b64decode(src.split(",", 1)[1]) == PNG_BYTES
    assert lines == []


def test_a_jpg_embeds_as_image_jpeg(tmp_path):
    photo = tmp_path / "flat.jpg"
    photo.write_bytes(PNG_BYTES)
    assert report.image_src(str(photo), "Flat A", lambda _m: None).startswith(
        "data:image/jpeg;base64,")


def test_no_image_at_all_falls_back_silently():
    lines, log = collect_log()
    assert report.image_src(None, "Flat A", log) is None
    assert report.image_src("", "Flat A", log) is None
    assert lines == []


@pytest.mark.parametrize("filename", ["flat.bmp", "flat.pdf"])
def test_an_unembeddable_extension_falls_back_and_says_why(tmp_path, filename):
    photo = tmp_path / filename
    photo.write_bytes(PNG_BYTES)
    lines, log = collect_log()
    assert report.image_src(str(photo), "Flat A", log) is None
    assert len(lines) == 1
    assert "Flat A" in lines[0]


def test_a_missing_file_falls_back_and_says_why(tmp_path):
    lines, log = collect_log()
    assert report.image_src(str(tmp_path / "nope.png"), "Flat A", log) is None
    assert len(lines) == 1
    assert "Flat A" in lines[0]


def test_an_oversize_image_falls_back_rather_than_bloating_the_report(tmp_path):
    """Three phone photos would otherwise turn a report into a 40 MB attachment."""
    photo = tmp_path / "huge.png"
    photo.write_bytes(b"\x00" * (report.MAX_EMBEDDED_IMAGE_BYTES + 1))
    lines, log = collect_log()
    assert report.image_src(str(photo), "Flat A", log) is None
    assert len(lines) == 1
    assert "Flat A" in lines[0]


def test_an_image_exactly_at_the_cap_is_still_embedded(tmp_path):
    photo = tmp_path / "big.png"
    photo.write_bytes(b"\x00" * report.MAX_EMBEDDED_IMAGE_BYTES)
    assert report.image_src(str(photo), "Flat A", lambda _m: None) is not None


def sample_frame(**overrides):
    row = {"name": "Flat A", "score": SAMPLE_SCORE, "rent_eur": 1250,
           "supermarkets": 8, "bakeries": 6, "pharmacies": 4, "gyms": 3,
           "transit_equiv_stops": 14.0, "green_area_m2": 35000, "dist_busy_road_m": 200,
           "Work_walk_min": 9.0, "lat": 52.0, "lon": 13.0}
    row.update(overrides)
    return pd.DataFrame([row])


DESTINATIONS = {"Work": {"coords": (52.0, 13.01),
                         "info": {"address": "2 Office Rd", "weight": 0.15}}}


def render(tmp_path, df=None, breakdowns=None, images=None, failed=()):
    df = sample_frame() if df is None else df
    breakdowns = {"Flat A": breakdown_for()} if breakdowns is None else breakdowns
    out = tmp_path / "overview.html"
    report.generate_report(df, breakdowns, DESTINATIONS, str(out), images,
                           failed_candidates=failed, log=lambda _m: None)
    return out.read_text(encoding="utf-8")


def test_the_report_is_a_complete_html_document(tmp_path):
    page = render(tmp_path)
    assert page.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in page
    assert "Flat A" in page


def test_a_flat_with_no_image_gets_a_score_dial_in_its_band(tmp_path):
    page = render(tmp_path)
    assert 'class="fs-dial' in page
    assert score_colour(SAMPLE_SCORE) in page
    assert "<img" not in page


def test_a_flat_with_an_image_gets_the_photo_instead_of_the_dial(tmp_path):
    page = render(tmp_path, images={"Flat A": "https://example.com/a.jpg"})
    assert '<img class="fs-photo" src="https://example.com/a.jpg"' in page
    assert 'class="fs-dial' not in page


def test_the_bars_rendered_sum_to_the_flats_score(tmp_path):
    """Parsed back out of the markup, so the assertion covers the rendering and
    not just bar_rows()."""
    page = render(tmp_path)
    fills = [float(x) for x in re.findall(r'fs-fill[^"]*" style="width:([0-9.]+)%', page)]
    assert sum(fills) / 100.0 * SCORE_SCALE_MAX == pytest.approx(SAMPLE_SCORE, abs=0.02)


def test_every_interpolated_value_is_escaped(tmp_path):
    df = sample_frame(name='Flat "A" <script>alert(1)</script>',
                      url='https://example.com/?a="b"')
    breakdowns = {'Flat "A" <script>alert(1)</script>': breakdown_for()}
    page = render(tmp_path, df=df, breakdowns=breakdowns,
                  images={'Flat "A" <script>alert(1)</script>': 'https://x.test/"y".jpg'})
    assert "<script>alert(1)</script>" not in page
    assert '?a="b"' not in page
    assert '"y".jpg' not in page


def test_a_listing_link_renders_only_when_the_flat_has_one(tmp_path):
    assert "View listing" not in render(tmp_path)
    page = render(tmp_path, df=sample_frame(url="https://example.com/expose/1"))
    assert "View listing" in page
    assert 'href="https://example.com/expose/1"' in page


def test_a_blank_url_cell_does_not_render_a_link(tmp_path):
    """`run()` fills the column with "" for flats that carry no link once any
    other flat does, and NaN is truthy - both must stay linkless."""
    assert "View listing" not in render(tmp_path, df=sample_frame(url=""))
    assert "View listing" not in render(tmp_path, df=sample_frame(url=float("nan")))


def test_commute_columns_become_readable_chips(tmp_path):
    page = render(tmp_path)
    assert "Work" in page
    assert "9.0 min" in page


def test_failed_candidates_are_named_in_the_footer(tmp_path):
    page = render(tmp_path, failed=[("Flat Z", "9 Nowhere Rd")])
    assert "Flat Z" in page
    assert "9 Nowhere Rd" in page


def test_no_footer_notice_when_every_candidate_geocoded(tmp_path):
    assert "could not be geocoded" not in render(tmp_path)


def test_the_exact_numbers_table_is_present_but_collapsed(tmp_path):
    page = render(tmp_path)
    assert "<details" in page
    assert "<table" in page
    # lat/lon are plumbing, not results - the ranked table never shows them.
    assert ">lat<" not in page


def test_osm_attribution_travels_with_the_report(tmp_path):
    """ODbL obligation, not decoration.

    This file is the one built to be sent to other people, so the notice has to
    be *in it* - the map gets folium's for free, and the README's does not
    travel with a detached HTML file.
    """
    page = render(tmp_path)
    assert "OpenStreetMap" in page
    assert "openstreetmap.org/copyright" in page
    assert "opendatacommons.org/licenses/odbl/" in page


def test_the_report_writes_the_file_and_logs_where(tmp_path):
    lines = []
    out = tmp_path / "sub" / "overview.html"
    out.parent.mkdir()
    report.generate_report(sample_frame(), {"Flat A": breakdown_for()}, DESTINATIONS,
                           str(out), None, log=lines.append)
    assert out.is_file()
    assert any(str(out) in line for line in lines)


def test_an_approximate_commute_is_marked_on_its_chip(tmp_path):
    """The report travels on its own, so the caveat has to travel with it."""
    df = sample_frame().rename(columns={"Work_walk_min": "Work_walk_min_approx"})
    page = render(tmp_path, df=df)

    assert "(approx.)" in page
    # The destination label must survive the longer suffix intact.
    assert "Work Approx" not in page
    assert ">Work " in page or "Work <strong>" in page


def test_a_routed_commute_carries_no_approximate_marker(tmp_path):
    assert "(approx.)" not in render(tmp_path)
