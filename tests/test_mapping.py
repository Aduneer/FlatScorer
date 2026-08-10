"""Map pin colours and the listing-link popup line.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import re

import pandas as pd
import pytest
from conftest import (
    make_scorer,
)

import flatscorer as fs

# ------------------------------------------------------------- map pin colours --

def map_frame(scores: list[float]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "name": f"Flat {i}", "score": s, "rent_eur": 1000, "supermarkets": 1,
            "bakeries": 1, "pharmacies": 1, "gyms": 1, "transit_stops": 1,
            "green_area_m2": 100, "dist_busy_road_m": 200,
            "lat": 52.52 + i * 0.001, "lon": 13.40,
        }
        for i, s in enumerate(scores)
    ]).sort_values("score", ascending=False)


def marker_colours(html: str) -> list[str]:
    return re.findall(r'markerColor": "([a-z]+)"', html)


@pytest.mark.parametrize("score,colour", [
    (9.5, "green"), (7.0, "green"), (6.0, "orange"), (4.0, "orange"),
    (3.0, "red"), (0.0, "red"),
])
def test_score_colour_uses_absolute_bands(score, colour):
    assert fs.score_colour(score) == colour


def test_map_colours_pins_by_absolute_score(tmp_path):
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([8.0, 5.0, 2.0]), {}, str(out))
    assert marker_colours(out.read_text()) == ["green", "orange", "red"]


def test_map_no_longer_stretches_a_narrow_field_across_the_whole_scale(tmp_path):
    # The old min-max colouring painted the worst candidate red and the best
    # green even for a 0.2-point spread, contradicting the sensitivity report
    # calling the same gap a tie. Alike scores must now look alike.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([5.1, 5.0, 4.9]), {}, str(out))
    assert set(marker_colours(out.read_text())) == {"orange"}


def test_map_notes_a_narrow_field_in_the_log(tmp_path, capsys):
    make_scorer().generate_map(map_frame([5.1, 5.0]), {}, str(tmp_path / "map.html"))
    out = capsys.readouterr().out
    assert "coloured by absolute score" in out
    assert "because they are alike" in out


def test_map_with_a_single_candidate_colours_it_on_the_same_absolute_scale(tmp_path):
    # No spread to normalize against - previously that made every lone candidate
    # red regardless of how good it was. A 9.0 is a green pin on its own.
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([9.0]), {}, str(out))
    assert marker_colours(out.read_text()) == ["green"]


def test_the_popup_link_is_empty_when_there_is_no_listing_url():
    assert fs._listing_link_html(None) == ""
    assert fs._listing_link_html("") == ""


def test_the_popup_link_escapes_the_url():
    """The popup is raw HTML in a file opened locally, and config.json is shared —
    so a link out of someone else's config must not be able to close the href."""
    link = fs._listing_link_html('https://example.com/a"><script>alert(1)</script>')
    assert '"><script>' not in link
    assert "&quot;&gt;&lt;script&gt;" in link


def test_the_map_popup_links_to_the_listing(tmp_path):
    frame = map_frame([8.0])
    frame["url"] = "https://example.com/expose/1"
    out = tmp_path / "map.html"
    make_scorer().generate_map(frame, {}, str(out))

    html = out.read_text()
    assert "View listing" in html
    assert "https://example.com/expose/1" in html


def test_the_map_popup_has_no_link_line_without_a_url(tmp_path):
    """A run with no links must produce the popup it always did."""
    out = tmp_path / "map.html"
    make_scorer().generate_map(map_frame([8.0]), {}, str(out))
    assert "View listing" not in out.read_text()
