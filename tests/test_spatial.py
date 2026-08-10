"""Projection, the search-area guard, and the spatial measurements.

Everything here runs offline — no Overpass, no Nominatim. Anything that would
touch the network is either not exercised or stubbed.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from conftest import (
    BERLIN_CRS,
    make_scorer,
    only_problem,
    valid_config,
)
from shapely.geometry import LineString, Point, Polygon

import flatscorer as fs
from flatscorer import geocode, osm

# ---------------------------------------------------------------- resolve_crs --

def test_resolve_crs_honours_an_explicit_override():
    scorer = make_scorer(parameters={"projected_crs": "EPSG:25832"})
    assert scorer.resolve_crs([52.5], [13.4]) == "EPSG:25832"


def test_resolve_crs_auto_detects_the_utm_zone():
    scorer = make_scorer(parameters={"projected_crs": "auto"})
    # Berlin sits in UTM zone 33N -> EPSG:32633.
    assert scorer.resolve_crs([52.52, 52.50], [13.40, 13.38]).endswith("32633")

# ------------------------------------------------------- spatial measurements --


def test_count_nearby_counts_only_features_inside_the_buffer():
    near = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    far = fs.to_point(52.5600, 13.4050, BERLIN_CRS)  # ~4.4 km north
    gdf = gpd.GeoDataFrame(geometry=[near, far], crs=BERLIN_CRS)
    assert fs.count_nearby(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500) == 1


def test_count_nearby_on_empty_input_is_zero():
    assert fs.count_nearby(52.52, 13.405, None, BERLIN_CRS) == 0
    assert fs.count_nearby(52.52, 13.405, gpd.GeoDataFrame(geometry=[]), BERLIN_CRS) == 0


def test_nearest_distance_m_measures_in_meters():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    road = LineString([(origin.x + 100, origin.y - 500), (origin.x + 100, origin.y + 500)])
    gdf = gpd.GeoDataFrame(geometry=[road], crs=BERLIN_CRS)
    assert fs.nearest_distance_m(52.5200, 13.4050, gdf, BERLIN_CRS) == pytest.approx(100, abs=1)


def test_nearest_distance_m_is_none_without_features():
    assert fs.nearest_distance_m(52.52, 13.405, gpd.GeoDataFrame(geometry=[]), BERLIN_CRS) is None


def test_green_area_and_points_separates_polygons_from_points():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    # A 100x100 m park fully inside the buffer, plus one point in and one out.
    park = Polygon([
        (origin.x, origin.y), (origin.x + 100, origin.y),
        (origin.x + 100, origin.y + 100), (origin.x, origin.y + 100),
    ])
    inside = Point(origin.x + 50, origin.y + 50)
    outside = Point(origin.x + 5000, origin.y)
    gdf = gpd.GeoDataFrame(geometry=[park, inside, outside], crs=BERLIN_CRS)

    area, count = fs.green_area_and_points(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500)
    assert area == pytest.approx(10_000, rel=0.01)
    assert count == 1


def test_green_area_and_points_clips_polygons_to_the_buffer():
    origin = fs.to_point(52.5200, 13.4050, BERLIN_CRS)
    # A huge forest - only the part within the 500 m buffer should count.
    forest = Polygon([
        (origin.x - 10_000, origin.y - 10_000), (origin.x + 10_000, origin.y - 10_000),
        (origin.x + 10_000, origin.y + 10_000), (origin.x - 10_000, origin.y + 10_000),
    ])
    gdf = gpd.GeoDataFrame(geometry=[forest], crs=BERLIN_CRS)
    area, count = fs.green_area_and_points(52.5200, 13.4050, gdf, BERLIN_CRS, dist=500)
    assert area == pytest.approx(3.14159 * 500 ** 2, rel=0.01)
    assert count == 0


def test_green_area_and_points_on_empty_input():
    assert fs.green_area_and_points(52.52, 13.405, None, BERLIN_CRS) == (0.0, 0)

# ------------------------------------------------------------ search area guard --

# Three points around Dupont Circle, plus one destination that is either in DC
# or - for the wrong-city case - in Berlin, Germany. UTM 18N covers DC.
DC_CRS = "EPSG:32618"
DC_CANDIDATES = {
    "candidate 'Flat A' (1500 Connecticut Ave NW)": (38.9097, -77.0434),
    "candidate 'Flat B' (2100 Pennsylvania Ave NW)": (38.9009, -77.0477),
    "candidate 'Flat C' (1400 14th St NW)": (38.9091, -77.0320),
}
DC_WORK = {"destination 'Work' (1600 Pennsylvania Ave NW)": (38.8977, -77.0365)}
WRONG_CITY_WORK = {"destination 'Work' (Unter den Linden)": (52.5170, 13.3889)}


def test_a_single_city_search_passes_the_guard():
    span = fs.check_search_area(dict(DC_CANDIDATES, **DC_WORK), DC_CRS,
                                centre_labels=DC_CANDIDATES)
    assert span < 2.0, "these addresses are all within ~1.5 km of each other"


def test_a_destination_in_the_wrong_city_is_rejected():
    with pytest.raises(fs.SearchAreaError) as excinfo:
        fs.check_search_area(dict(DC_CANDIDATES, **WRONG_CITY_WORK), DC_CRS,
                             centre_labels=DC_CANDIDATES)
    assert excinfo.value.span_km > 1000


def test_the_message_names_the_offending_destination():
    """'bbox too large' sends the user back to guessing; the address does not."""
    with pytest.raises(fs.SearchAreaError) as excinfo:
        fs.check_search_area(dict(DC_CANDIDATES, **WRONG_CITY_WORK), DC_CRS,
                             centre_labels=DC_CANDIDATES)
    message = str(excinfo.value)
    assert "destination 'Work'" in message
    assert "Unter den Linden" in message
    assert "Flat A" not in message, "the candidates are not the problem"
    assert "max_bbox_span_km" in message, "the message has to say how to override it"


def test_the_outlier_is_measured_from_the_candidates_not_from_everything():
    """Measured from the candidates, the outlier accounts for the whole span.

    Measured from the midpoint of *all* the points it would account for only a
    fraction of it - the wrong address would drag the reported centre towards
    itself and then look less far from it than it is.
    """
    points = dict(DC_CANDIDATES, **WRONG_CITY_WORK)

    def distance_reported(**kwargs) -> fs.SearchAreaError:
        with pytest.raises(fs.SearchAreaError) as excinfo:
            fs.check_search_area(points, DC_CRS, **kwargs)
        return excinfo.value

    from_candidates = distance_reported(centre_labels=DC_CANDIDATES)
    from_everything = distance_reported()

    assert from_candidates.outlier in WRONG_CITY_WORK
    # Three DC points against one in Berlin: the wrong address pulls the overall
    # midpoint a quarter of the way towards itself, and so understates its own
    # distance by that much. Anchoring on the candidates reports the full gap.
    assert from_candidates.outlier_km == pytest.approx(from_everything.outlier_km * 4 / 3, rel=0.02)


def test_raising_the_threshold_lets_a_large_search_through():
    points = dict(DC_CANDIDATES, **WRONG_CITY_WORK)
    span = fs.check_search_area(points, DC_CRS, max_span_km=10_000,
                                centre_labels=DC_CANDIDATES)
    assert span > 1000


def test_the_span_is_measured_in_metres_not_degrees():
    """A degree of longitude is ~68 km at 52 deg N, not 111 km - the bug this
    guard must not repeat. 0.5 deg apart is ~34 km there, over a 30 km limit but
    under it if the box is (wrongly) judged in degrees scaled by 111 km... and
    well over if judged the other way. Pin the honest number."""
    points = {"candidate 'A' (west)": (52.0, 13.0), "candidate 'B' (east)": (52.0, 13.5)}
    span = fs.check_search_area(points, BERLIN_CRS, max_span_km=100)
    assert span == pytest.approx(34.2, rel=0.02)


def test_an_empty_point_set_spans_nothing():
    assert fs.check_search_area({}, DC_CRS) == 0.0


def test_the_guard_falls_back_to_all_points_when_no_centre_is_given():
    with pytest.raises(fs.SearchAreaError) as excinfo:
        fs.check_search_area(dict(DC_CANDIDATES, **WRONG_CITY_WORK), DC_CRS)
    # Centre is now the midpoint of the four, so both ends are ~3,200 km out and
    # the naming is arbitrary - but it must still raise rather than crash.
    assert excinfo.value.outlier is not None


def test_max_bbox_span_km_is_validated():
    assert "max_bbox_span_km" in only_problem(valid_config(parameters={"max_bbox_span_km": 0}))


def test_search_area_error_is_a_value_error():
    """The GUI catches broad Exception; the CLI relies on ValueError."""
    assert issubclass(fs.SearchAreaError, ValueError)


def test_run_rejects_a_wrong_city_destination_before_downloading(monkeypatch):
    """The whole point is failing *before* the multi-minute download, not after."""
    coords = {
        "1 Main St": (38.9097, -77.0434),
        "2 Office Rd": (52.5170, 13.3889),
    }
    monkeypatch.setattr(geocode, "geocode_safe", lambda addr, label, **kw: coords[addr])

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("started an OpenStreetMap download despite an implausible bbox")

    monkeypatch.setattr(osm, "query_with_retry", must_not_be_called)

    scorer = fs.FlatScorer(valid_config(), verbose=False)
    with pytest.raises(fs.SearchAreaError) as excinfo:
        scorer.run()
    assert "2 Office Rd" in str(excinfo.value)


def test_run_accepts_a_same_city_config(monkeypatch):
    """The guard must not fire on an ordinary search; stop at the download."""
    coords = {
        "1 Main St": (38.9097, -77.0434),
        "2 Office Rd": (38.8977, -77.0365),
    }
    monkeypatch.setattr(geocode, "geocode_safe", lambda addr, label, **kw: coords[addr])
    monkeypatch.setattr(osm, "query_with_retry", lambda fn, **kw: (_ for _ in ()).throw(
        RuntimeError("reached the download")))

    scorer = fs.FlatScorer(valid_config(), verbose=False)
    with pytest.raises(RuntimeError, match="reached the download"):
        scorer.run()
