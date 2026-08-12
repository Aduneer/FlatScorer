"""Projection, the search-area guard, and the per-candidate spatial measurements.

`check_search_area` raises before any download, for the same reason
`validate_config` does: a mis-geocoded address otherwise becomes a multi-minute
Overpass hang rather than an error.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import geopandas as gpd
from shapely.geometry import Point

# Largest side of the search bounding box, in km, that will be downloaded without
# complaint. Every resolved point has to fit inside this, so it is really a guard
# against a mis-geocoded address: "work" landing in the wrong Berlin produces a
# box hundreds of km across, and asking Overpass for that much pedestrian network
# is a long hang followed by a rejection. 30 km comfortably covers any real
# single-city search (greater London is ~45 km east-west; a config that genuinely
# spans that raises the parameter).
DEFAULT_MAX_BBOX_SPAN_KM = 30.0


class SearchAreaError(ValueError):
    """The area to download is too large to be a plausible apartment search.

    Raised before the Overpass call rather than after it: the whole complaint is
    that a mis-geocoded address makes the tool hang for minutes on a download
    that was never going to succeed. Like `ConfigError` this is a hard error and
    not a prompt, because `run()` is driven by the GUI as well as the CLI and
    Streamlit has no interactive channel to answer one.
    """

    def __init__(self, span_km: float, max_span_km: float, outlier: str | None, outlier_km: float):
        self.span_km = span_km
        self.max_span_km = max_span_km
        self.outlier = outlier
        self.outlier_km = outlier_km

        message = (f"The search area spans {span_km:.0f} km, over the "
                   f"{max_span_km:g} km limit.")
        if outlier:
            message += (f" {outlier} sits {outlier_km:.0f} km from the middle of the "
                        "candidate apartments, so it has most likely geocoded to the "
                        "wrong place - check that address first.")
        message += (" Downloading a street network this size would hang for a long time "
                    "and then be refused by Overpass. If the search really is this large, "
                    "raise parameters['max_bbox_span_km'].")
        super().__init__(message)


def resolve_crs(lats: list[float], lons: list[float], configured: str | None = None) -> str:
    """Determine the projected CRS (e.g. UTM zone) used for metric calculations.

    An explicit `configured` value wins, unless it is the sentinel "auto".
    Returns the string only; the caller decides whether to announce it, since
    only the auto-detected case is worth a log line.
    """
    if configured and configured.lower() != "auto":
        return configured

    # Auto-detect using GeoPandas estimate_utm_crs
    points_gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
    return points_gdf.estimate_utm_crs().to_string()


def to_point(lat: float, lon: float, crs: str) -> Point:
    """Convert (lat, lon) in WGS84 to a projected Point geometry."""
    return gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs).iloc[0]


def check_search_area(points: dict[str, tuple[float, float]], crs: str,
                      max_span_km: float = DEFAULT_MAX_BBOX_SPAN_KM,
                      centre_labels: Iterable[str] | None = None) -> float:
    """Return the search area's longest side in km, raising if it exceeds the limit.

    `points` maps a display label ("candidate 'Flat A' (1 Main St)") to its
    resolved (lat, lon). The span is measured in `crs`, not in degrees: a degree
    of longitude is ~68 km at 52 deg N and ~111 km at the equator, so a threshold
    in degrees would mean a different thing in every city.

    `centre_labels` names the points that define where the search is *supposed*
    to be - the candidates. Measuring the outlier from their midpoint rather than
    from the midpoint of everything is what makes the message name the one wrong
    address instead of splitting the blame between the two ends of the box.

    On a wildly wrong search the auto-detected UTM zone fits neither end and the
    reported km overstate the true distance (a DC/Berlin mix reads ~6,900 km for
    a real ~6,400). That only ever errs towards rejecting, and by then the number
    is absurd either way; near the threshold, where it matters, it is accurate.
    """
    if not points:
        return 0.0

    labels = list(points)
    projected = gpd.GeoSeries(
        [Point(lon, lat) for lat, lon in (points[label] for label in labels)],
        crs="EPSG:4326",
    ).to_crs(crs)
    xs = list(projected.x)
    ys = list(projected.y)

    span_km = max(max(xs) - min(xs), max(ys) - min(ys)) / 1000.0
    if span_km <= max_span_km:
        return span_km

    # Only reached on the failure path, so the extra pass costs nothing in the
    # normal case.
    centre_set = set(centre_labels) if centre_labels else set()
    centre_indices = [i for i, label in enumerate(labels) if label in centre_set] or list(range(len(labels)))
    centre_x = sum(xs[i] for i in centre_indices) / len(centre_indices)
    centre_y = sum(ys[i] for i in centre_indices) / len(centre_indices)

    distances = [math.hypot(x - centre_x, y - centre_y) / 1000.0 for x, y in zip(xs, ys)]
    farthest = max(range(len(labels)), key=lambda i: distances[i])
    raise SearchAreaError(span_km, max_span_km, labels[farthest], distances[farthest])


def count_nearby(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str, dist: float = 500,
                 weight_col: str | None = None) -> float:
    """Count features within `dist` meters of a given coordinate.

    With `weight_col`, sums that column instead of the rows - so a layer whose
    members are not interchangeable (transit stops, where a metro station is
    worth more than a bus stop) can contribute what each one is actually worth.
    Returns an `int` when unweighted, so every caller that just wants a count is
    unaffected.
    """
    if gdf_proj is None or len(gdf_proj) == 0:
        return 0
    buf = to_point(lat, lon, crs).buffer(dist)
    within = gdf_proj.intersects(buf)
    if weight_col is None or weight_col not in gdf_proj.columns:
        return int(within.sum())
    return float(gdf_proj.loc[within, weight_col].fillna(0).sum())


def nearest_distance_m(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str) -> float | None:
    """Find distance in meters to the nearest feature in `gdf_proj`."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return None
    return float(gdf_proj.distance(to_point(lat, lon, crs)).min())


def green_area_and_points(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str, dist: float = 500) -> tuple[float, int]:
    """Calculate total green polygon area (m²) and green point count within `dist` meters."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return 0.0, 0
    buf = to_point(lat, lon, crs).buffer(dist)
    geom_type = gdf_proj.geometry.type
    polys = gdf_proj[geom_type.isin(["Polygon", "MultiPolygon"])]
    pts = gdf_proj[geom_type == "Point"]
    area_m2 = polys.geometry.intersection(buf).area.sum() if len(polys) else 0.0
    point_count = int(pts.intersects(buf).sum()) if len(pts) else 0
    return float(area_m2), point_count


def straight_line_distance_m(orig: tuple[float, float], dest: tuple[float, float], crs: str | None = None) -> float:
    """Straight-line distance in meters between two (lat, lon) pairs.

    With a projected `crs` this is a true metric distance. Without one it falls
    back to scaling degrees by 111 km, which overstates east-west distance away
    from the equator (~60% too long at 52 deg N) - so callers should pass the CRS.
    """
    if crs:
        return float(to_point(*orig, crs).distance(to_point(*dest, crs)))
    return float(Point(orig[1], orig[0]).distance(Point(dest[1], dest[0])) * 111000)
