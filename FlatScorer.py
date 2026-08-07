#!/usr/bin/env python3
"""
FlatScorer — Multi-criteria apartment scoring tool.

Scores candidate apartments based on nearby amenities, transit access,
green space, road-noise proximity, walking commute to user-defined
destinations, and rent — producing a ranked comparison table, CSV
export, interactive Folium map, and weight-sensitivity analysis.

Usage:
    python FlatScorer.py --generate-config config.json
    python FlatScorer.py --config config.json

See README.md for full documentation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import folium
import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import requests
from osmnx._errors import InsufficientResponseError
from shapely.geometry import Point

# Configure default OSMnx settings
ox.settings.use_cache = True
ox.settings.log_console = False

# Identify this tool to Nominatim, per its usage policy:
# https://operations.osmfoundation.org/policies/nominatim/
ox.settings.useragent = "FlatScorer (github.com/Aduneer/FlatScorer)"

# Fallback Overpass API mirrors in case overpass-api.de has backend downtime
DEFAULT_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.private.coffee/api",
]

# Winner/runner-up score gap below which the top two are reported as effectively tied
NARROW_MARGIN_THRESHOLD = 0.5

# Nominatim's usage policy caps clients at 1 request/second, and osmnx does not
# throttle for us: https://operations.osmfoundation.org/policies/nominatim/
NOMINATIM_MIN_INTERVAL_S = 1.0

# Geocoding, like Overpass, fails transiently. Retry a couple of times before
# dropping a candidate from the ranking entirely.
GEOCODE_ATTEMPTS = 3
GEOCODE_BACKOFF_S = 2.0

# Monotonic timestamp of the last Nominatim request, shared process-wide so the
# rate limit holds across the candidate and destination loops (and across runs
# in the long-lived Streamlit process).
_last_geocode_at = 0.0

# Default configuration template (Generic demo using Washington, DC landmarks)
DEFAULT_CONFIG = {
    "candidates": [
        {
            "name": "Flat A - Dupont Circle",
            "address": "1500 Connecticut Ave NW, Washington, DC 20036, USA",
            "rent": 1800
        },
        {
            "name": "Flat B - Foggy Bottom",
            "address": "2100 Pennsylvania Avenue NW, Washington, DC 20037, USA",
            "rent": 2100
        },
        {
            "name": "Flat C - Logan Circle",
            "address": "1400 14th St NW, Washington, DC 20005, USA",
            "rent": 1950
        }
    ],
    "destinations": {
        "White House": {
            "address": "1600 Pennsylvania Ave NW, Washington, DC 20500, USA",
            "weight": 0.15,
            "icon": "landmark",
            "color": "blue"
        },
        "Union Station": {
            "address": "50 Massachusetts Ave NE, Washington, DC 20002, USA",
            "weight": 0.15,
            "icon": "train",
            "color": "red"
        }
    },
    "weights": {
        "supermarket": 0.30,
        "bakery": 0.10,
        "pharmacy": 0.15,
        "gym": 0.15,
        "transit": 0.33,
        "green": 0.05,
        "noise": 0.05,
        "rent": 0.25
    },
    "parameters": {
        "euros_per_extra_minute": 20,
        "buffer_m": 500,
        "noise_cap_m": 200,
        "projected_crs": "auto",
        "show_walk_routes": True
    },
    "output": {
        "csv_file": "apartment_scores.csv",
        "html_file": "apartment_map.html"
    }
}


def query_with_retry(fn, mirrors=DEFAULT_OVERPASS_MIRRORS, retries_per_mirror=2, backoff_s=3):
    """Run an Overpass-backed OSMnx call with automatic fallback across mirrors.

    Mirror selection is scoped to this call: `ox.settings.overpass_url` is a
    process-wide global, so leaving it pointed at whichever mirror happened to
    answer last would silently carry over into later runs - which matters in the
    long-lived Streamlit process, not just across CLI invocations.
    """
    original_url = ox.settings.overpass_url
    last_err = None
    try:
        for mirror in mirrors:
            ox.settings.overpass_url = mirror
            for attempt in range(1, retries_per_mirror + 1):
                try:
                    return fn()
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.HTTPError,
                        ConnectionError,
                        TimeoutError) as e:
                    last_err = e
                    print(f"[!] {mirror} attempt {attempt}/{retries_per_mirror} failed: {e}")
                    time.sleep(backoff_s)
            print(f"[!] Giving up on {mirror}, trying next mirror...")
    finally:
        ox.settings.overpass_url = original_url

    raise RuntimeError(
        "All Overpass mirrors failed. Check your network connection or OSM status."
    ) from last_err


def _throttle_geocode():
    """Block until at least NOMINATIM_MIN_INTERVAL_S has passed since the last geocode."""
    global _last_geocode_at
    wait = NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _last_geocode_at)
    if wait > 0:
        time.sleep(wait)
    _last_geocode_at = time.monotonic()


def geocode_safe(address: str, label: str, attempts: int = GEOCODE_ATTEMPTS) -> tuple[float, float] | None:
    """Safely geocode an address into (latitude, longitude) tuple, with rate limiting and retries."""
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        _throttle_geocode()
        try:
            return ox.geocode(address)
        except InsufficientResponseError as e:
            # Nominatim answered, it just has no match - retrying cannot help.
            last_err = e
            break
        except Exception as e:  # noqa: BLE001 - geocoding can fail from network errors or osmnx's own exceptions; drop this candidate instead of crashing the run
            last_err = e
            if attempt < attempts:
                print(f"[!] Geocoding '{label}' failed (attempt {attempt}/{attempts}): {e} - retrying...")
                time.sleep(GEOCODE_BACKOFF_S * attempt)

    print(f"[!] Couldn't geocode '{label}': {address} ({last_err})")
    print("    Ensure the address includes street, house number, postal code, and city.")
    return None


def rent_time_equivalent(rent: float, euros_per_minute: float) -> float:
    """Convert rent into the commute minutes a tenant would trade for it.

    Puts rent on the same scale as walking time so both can be weighted together.
    A non-positive euros-per-minute means "rent is not being traded against time",
    so the penalty collapses to zero rather than dividing by zero.
    """
    if euros_per_minute <= 0:
        return 0.0
    return rent / euros_per_minute


def _winner_margin(scores: dict[str, float]) -> float | None:
    """Return the score gap between the top pick and the runner-up, or None if fewer than two."""
    if len(scores) < 2:
        return None
    ranked = sorted(scores.values(), reverse=True)
    return ranked[0] - ranked[1]


def safe_filter(gdf: gpd.GeoDataFrame, col: str, val: Any) -> gpd.GeoDataFrame:
    """Filter a GeoDataFrame by column value(s), returning an empty GeoDataFrame if column missing."""
    if gdf is None or len(gdf) == 0 or col not in gdf.columns:
        return gdf.iloc[0:0] if gdf is not None else gpd.GeoDataFrame()
    if isinstance(val, list):
        return gdf[gdf[col].isin(val)]
    return gdf[gdf[col] == val]


def to_point(lat: float, lon: float, crs: str) -> Point:
    """Convert (lat, lon) in WGS84 to a projected Point geometry."""
    return gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326").to_crs(crs).iloc[0]


def count_nearby(lat: float, lon: float, gdf_proj: gpd.GeoDataFrame, crs: str, dist: float = 500) -> int:
    """Count features within `dist` meters of a given coordinate."""
    if gdf_proj is None or len(gdf_proj) == 0:
        return 0
    buf = to_point(lat, lon, crs).buffer(dist)
    return int(gdf_proj.intersects(buf).sum())


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


def walk_route(G: nx.MultiDiGraph, orig: tuple[float, float], dest: tuple[float, float], walking_speed_m_per_min: float = 83.33, projected_crs: str | None = None) -> tuple[float, list[tuple[float, float]]]:
    """Calculate walking time in minutes and the shortest-path route (list of lat/lon points) between two coordinates over OSM graph G (~5 km/h = 83.33 m/min)."""
    orig_node = ox.distance.nearest_nodes(G, orig[1], orig[0])
    dest_node = ox.distance.nearest_nodes(G, dest[1], dest[0])
    try:
        path = nx.shortest_path(G, orig_node, dest_node, weight="length")
        length_m = nx.path_weight(G, path, weight="length")
        route = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in path]
        return length_m / walking_speed_m_per_min, route
    except nx.NetworkXNoPath:
        print(f"[!] Warning: No walking path found between {orig} and {dest}. Defaulting to straight-line distance.")
        dist_m = straight_line_distance_m(orig, dest, projected_crs)
        return dist_m / walking_speed_m_per_min, [orig, dest]


class FlatScorer:
    """Multi-criteria apartment scoring engine."""

    def __init__(self, config: dict[str, Any], verbose: bool = True):
        self.config = config
        self.verbose = verbose

        self.candidates_raw = config.get("candidates", [])
        self.destinations_config = config.get("destinations", {})
        self.weights = config.get("weights", {})
        self.params = config.get("parameters", {})
        self.output_config = config.get("output", {})

        self.euros_per_min = self.params.get("euros_per_extra_minute", 20)
        self.buffer_m = self.params.get("buffer_m", 500)
        self.noise_cap_m = self.params.get("noise_cap_m", 200)
        self.configured_crs = self.params.get("projected_crs", "auto")
        self.show_walk_routes = self.params.get("show_walk_routes", True)

        # Populated by run(): (name, address) pairs that failed to geocode and
        # were therefore dropped from the ranking. Callers (e.g. the GUI) read
        # this to surface silent losses instead of quietly ranking fewer flats.
        self.failed_candidates: list[tuple[str, str]] = []
        self.failed_destinations: list[tuple[str, str]] = []

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def resolve_crs(self, lats: list[float], lons: list[float]) -> str:
        """Determine projected CRS (e.g. UTM zone) for metric calculations."""
        if self.configured_crs and self.configured_crs.lower() != "auto":
            return self.configured_crs

        # Auto-detect using GeoPandas estimate_utm_crs
        points_gdf = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lons, lats), crs="EPSG:4326")
        estimated_crs = points_gdf.estimate_utm_crs().to_string()
        self._log(f"[+] Auto-detected optimal projected CRS: {estimated_crs}")
        return estimated_crs

    def compute_score(self, m: dict[str, Any], weights: dict[str, float]) -> float:
        """Calculate total score based on metrics dictionary and weight vector."""
        score = (
            weights.get("supermarket", 0.30) * m.get("supermarket_count", 0)
            + weights.get("bakery", 0.10) * m.get("bakery_count", 0)
            + weights.get("pharmacy", 0.15) * m.get("pharmacy_count", 0)
            + weights.get("gym", 0.15) * m.get("gym_count", 0)
            + weights.get("transit", 0.33) * m.get("transit_count", 0)
            + weights.get("green", 0.05) * m.get("green_score", 0)
            + weights.get("noise", 0.05) * m.get("noise_benefit", 0)
            - weights.get("rent", 0.25) * m.get("rent_time_equiv", 0)
        )

        # Subtract weighted destination walk times
        dest_times = m.get("destinations_min", {})
        for dest_name, mins in dest_times.items():
            dest_weight = self.destinations_config.get(dest_name, {}).get("weight", 0.15)
            # Check if custom weight for destination is specified in weights dict
            if f"dest_{dest_name}" in weights:
                dest_weight = weights[f"dest_{dest_name}"]
            score -= dest_weight * mins

        return score

    def run_sensitivity_check(self, metrics_by_name: dict[str, dict[str, Any]], perturb: float = 0.2):
        """Run sensitivity analysis by nudging weights by +/- perturb% and observing winner stability."""
        baseline_scores = {n: self.compute_score(m, self.weights) for n, m in metrics_by_name.items()}
        if not baseline_scores:
            return

        baseline_top = max(baseline_scores, key=baseline_scores.get)
        baseline_margin = _winner_margin(baseline_scores)

        self._log(f"\nSensitivity check (each weight nudged +/-{int(perturb*100)}%, baseline winner: {baseline_top})")
        if baseline_margin is not None:
            self._log(f"Baseline margin over runner-up: {baseline_margin:.2f} points")
        self._log("-" * 75)

        # Build list of active weight keys
        active_weights = dict(self.weights)
        for dest_name in self.destinations_config:
            weight_key = f"dest_{dest_name}"
            if weight_key not in active_weights:
                active_weights[weight_key] = self.destinations_config[dest_name].get("weight", 0.15)

        any_flip = False
        narrowest_margin = baseline_margin
        for key, base_val in active_weights.items():
            for factor, label in [(1 + perturb, f"+{int(perturb*100)}%"), (1 - perturb, f"-{int(perturb*100)}%")]:
                w2 = dict(active_weights)
                w2[key] = base_val * factor
                scores2 = {n: self.compute_score(m, w2) for n, m in metrics_by_name.items()}
                new_top = max(scores2, key=scores2.get)
                margin = _winner_margin(scores2)
                if margin is not None and (narrowest_margin is None or margin < narrowest_margin):
                    narrowest_margin = margin
                flipped = new_top != baseline_top
                any_flip = any_flip or flipped
                marker = "  <- winner changes!" if flipped else ""
                margin_txt = f" (margin {margin:.2f})" if margin is not None else ""
                self._log(f"  {key:18s} {label:5s}  ->  top pick: {new_top}{margin_txt}{marker}")

        self._log("-" * 75)
        if not any_flip:
            self._log(f"Ranking is stable across all +/-{int(perturb*100)}% weight nudges - your top pick is robust.")
        else:
            self._log("Some weight changes flip the top pick - consider reviewing those criteria weights carefully.")

        if narrowest_margin is not None:
            self._log(f"Narrowest winner/runner-up gap seen: {narrowest_margin:.2f} points")
            if narrowest_margin < NARROW_MARGIN_THRESHOLD:
                self._log(f"[!] That is under {NARROW_MARGIN_THRESHOLD:.1f} points - the top two are effectively tied; "
                          "treat the ranking as a toss-up rather than a clear winner.")

    def run(self) -> pd.DataFrame:
        """Execute geocoding, spatial network fetching, metric calculation, and reporting."""
        self._log("Geocoding candidate apartment addresses...")
        self.failed_candidates = []
        self.failed_destinations = []
        resolved_candidates = {}
        for candidate in self.candidates_raw:
            name = candidate["name"]
            addr = candidate["address"]
            rent = candidate["rent"]
            coords = geocode_safe(addr, name)
            if coords:
                resolved_candidates[name] = {"coords": coords, "rent": rent, "address": addr, "raw": candidate}
            else:
                self.failed_candidates.append((name, addr))

        if not resolved_candidates:
            raise ValueError("No valid candidate apartment addresses could be geocoded.")

        if self.failed_candidates:
            print(f"\n[!] {len(self.failed_candidates)} candidate(s) dropped - they could not be geocoded "
                  "and are MISSING from the ranking:")
            for name, addr in self.failed_candidates:
                print(f"      - {name}: {addr}")

        self._log("\nGeocoding destination locations...")
        resolved_destinations = {}
        for dest_name, dest_info in self.destinations_config.items():
            addr = dest_info["address"]
            coords = geocode_safe(addr, dest_name)
            if coords:
                resolved_destinations[dest_name] = {
                    "coords": coords,
                    "info": dest_info
                }
            else:
                self.failed_destinations.append((dest_name, addr))
                print(f"[!] Warning: Destination '{dest_name}' could not be geocoded and will be skipped.")

        # Determine bounding box
        all_coords = [v["coords"] for v in resolved_candidates.values()] + [v["coords"] for v in resolved_destinations.values()]
        lats = [c[0] for c in all_coords]
        lons = [c[1] for c in all_coords]

        # Resolve projected CRS (UTM or user configured)
        projected_crs = self.resolve_crs(lats, lons)

        margin = 0.015
        bbox = (min(lons) - margin, min(lats) - margin, max(lons) + margin, max(lats) + margin)

        self._log("\nDownloading street walking network from OpenStreetMap...")
        def get_graph():
            return ox.graph_from_bbox(bbox=bbox, network_type="walk")

        def get_pois():
            tags = {
                "shop": ["supermarket", "bakery"],
                "amenity": ["pharmacy"],
                "leisure": ["fitness_centre", "park"],
                "landuse": ["grass", "forest"],
                "railway": ["tram_stop"],
                "highway": ["bus_stop", "primary", "secondary"],
            }
            return ox.features_from_bbox(bbox=bbox, tags=tags)

        G = query_with_retry(get_graph)
        self._log("Downloading points of interest (POIs) from OpenStreetMap...")
        pois = query_with_retry(get_pois)

        supermarkets = safe_filter(pois, "shop", "supermarket")
        bakeries     = safe_filter(pois, "shop", "bakery")
        pharmacies   = safe_filter(pois, "amenity", "pharmacy")
        gyms         = safe_filter(pois, "leisure", "fitness_centre")
        parks        = safe_filter(pois, "leisure", "park")
        greenland    = safe_filter(pois, "landuse", ["grass", "forest"])
        green_all    = pd.concat([gdf for gdf in [parks, greenland] if not gdf.empty]) if not (parks.empty and greenland.empty) else gpd.GeoDataFrame()
        bus          = safe_filter(pois, "highway", "bus_stop")
        tram         = safe_filter(pois, "railway", "tram_stop")
        transit      = pd.concat([gdf for gdf in [bus, tram] if not gdf.empty]) if not (bus.empty and tram.empty) else gpd.GeoDataFrame()
        busy_roads   = safe_filter(pois, "highway", ["primary", "secondary"])

        def proj(gdf):
            return gdf.to_crs(projected_crs) if (gdf is not None and len(gdf) > 0) else gdf

        supermarkets_p, bakeries_p, pharmacies_p, gyms_p, transit_p, green_p, roads_p = map(
            proj, [supermarkets, bakeries, pharmacies, gyms, transit, green_all, busy_roads]
        )

        self._log("\nScoring candidates...")
        metrics_by_name = {}
        routes_by_candidate = {}
        rows = []

        for name, info in resolved_candidates.items():
            lat, lon = info["coords"]
            rent = info["rent"]

            green_area_m2, green_points = green_area_and_points(lat, lon, green_p, projected_crs, dist=self.buffer_m)
            dist_to_busy_road = nearest_distance_m(lat, lon, roads_p, projected_crs)
            effective_road_dist = dist_to_busy_road if dist_to_busy_road is not None else self.noise_cap_m
            noise_benefit = min(effective_road_dist, self.noise_cap_m) / 20.0

            dest_times = {}
            dest_routes = {}
            for dest_name, dest_data in resolved_destinations.items():
                dest_coords = dest_data["coords"]
                dest_times[dest_name], dest_routes[dest_name] = walk_route(
                    G, (lat, lon), dest_coords, projected_crs=projected_crs
                )
            routes_by_candidate[name] = dest_routes

            m = {
                "supermarket_count": count_nearby(lat, lon, supermarkets_p, projected_crs, dist=self.buffer_m),
                "bakery_count":      count_nearby(lat, lon, bakeries_p, projected_crs, dist=self.buffer_m),
                "pharmacy_count":    count_nearby(lat, lon, pharmacies_p, projected_crs, dist=self.buffer_m),
                "gym_count":         count_nearby(lat, lon, gyms_p, projected_crs, dist=self.buffer_m),
                "transit_count":     count_nearby(lat, lon, transit_p, projected_crs, dist=self.buffer_m),
                "green_score":       green_area_m2 / 1000.0 + green_points * 0.5,
                "noise_benefit":     noise_benefit,
                "destinations_min":  dest_times,
                "rent_time_equiv":   rent_time_equivalent(rent, self.euros_per_min),
            }
            metrics_by_name[name] = m
            score = self.compute_score(m, self.weights)

            row = {
                "name": name,
                "score": round(score, 2),
                "rent_eur": rent,
                "supermarkets": m["supermarket_count"],
                "bakeries": m["bakery_count"],
                "pharmacies": m["pharmacy_count"],
                "gyms": m["gym_count"],
                "transit_stops": m["transit_count"],
                "green_area_m2": round(green_area_m2),
                "dist_busy_road_m": round(effective_road_dist),
            }

            for dest_name, mins in dest_times.items():
                clean_dest_key = dest_name.lower().replace(" ", "_")
                row[f"{clean_dest_key}_walk_min"] = round(mins, 1)

            row["lat"] = lat
            row["lon"] = lon
            rows.append(row)

        df = pd.DataFrame(rows).sort_values("score", ascending=False)
        self._log("\n" + df.drop(columns=["lat", "lon"]).to_string(index=False))

        csv_file = self.output_config.get("csv_file", "apartment_scores.csv")
        df.to_csv(csv_file, index=False)
        self._log(f"\n[+] Saved score table to {csv_file}")

        self.run_sensitivity_check(metrics_by_name)

        # Generate Folium Map
        html_file = self.output_config.get("html_file", "apartment_map.html")
        self.generate_map(df, resolved_destinations, html_file, routes_by_candidate)

        return df

    def generate_map(self, df: pd.DataFrame, resolved_destinations: dict[str, Any], html_file: str, routes_by_candidate: dict[str, dict[str, list[tuple[float, float]]]] | None = None):
        """Generate interactive Folium map with candidate apartments, destination pins, and predicted walking routes."""
        routes_by_candidate = routes_by_candidate or {}
        first_lat = df.iloc[0]["lat"]
        first_lon = df.iloc[0]["lon"]
        m_map = folium.Map(location=[first_lat, first_lon], zoom_start=13)
        route_group = folium.FeatureGroup(name="Predicted walking routes", show=self.show_walk_routes)

        # Add destinations to map
        for dest_name, dest_data in resolved_destinations.items():
            coords = dest_data["coords"]
            info = dest_data["info"]
            icon_name = info.get("icon", "star")
            icon_color = info.get("color", "blue")
            folium.Marker(
                coords,
                tooltip=dest_name,
                popup=f"<b>Destination: {dest_name}</b><br>{info.get('address', '')}",
                icon=folium.Icon(color=icon_color, icon=icon_name, prefix="fa"),
            ).add_to(m_map)

        max_score, min_score = df["score"].max(), df["score"].min()
        score_spread = float(max_score - min_score)
        score_range = score_spread if (max_score != min_score) else 1.0

        # Min-max colouring always paints the worst candidate red and the best
        # green, even when the whole set is a photo finish. Below the same
        # threshold the sensitivity check calls a tie, drop the colour scale
        # entirely rather than inventing a contrast the scores don't support.
        scores_are_tied = len(df) > 1 and score_spread < NARROW_MARGIN_THRESHOLD
        if scores_are_tied:
            self._log(f"[i] Candidate scores span only {score_spread:.2f} points "
                      f"(under {NARROW_MARGIN_THRESHOLD:.1f}) - map pins are shown in one neutral colour "
                      "instead of a red-to-green scale.")

        for _, row in df.iterrows():
            if scores_are_tied:
                color = "cadetblue"
            else:
                frac = (row["score"] - min_score) / (score_range + 1e-9)
                color = "green" if frac > 0.66 else "orange" if frac > 0.33 else "red"

            dest_lines = []
            for col in df.columns:
                if col.endswith("_walk_min"):
                    dest_label = col.replace("_walk_min", "").replace("_", " ").title()
                    dest_lines.append(f"{dest_label}: {row[col]} min")
            dest_html = " | ".join(dest_lines)

            popup = (
                f"<b>{row['name']}</b><br>"
                f"Score: {row['score']}<br>"
                f"Rent: €{row['rent_eur']}<br>"
                f"Commute: {dest_html}<br>"
                f"Supermarkets: {row['supermarkets']} | Bakeries: {row['bakeries']}<br>"
                f"Pharmacies: {row['pharmacies']} | Gyms: {row['gyms']}<br>"
                f"Transit stops: {row['transit_stops']}<br>"
                f"Green area nearby: {row['green_area_m2']} m²<br>"
                f"Distance to busy road: {row['dist_busy_road_m']} m"
            )
            if scores_are_tied:
                popup += (f"<br><i>All candidates score within {score_spread:.2f} points - "
                          "pin colour carries no ranking information here.</i>")
            folium.Marker(
                [row["lat"], row["lon"]],
                tooltip=f"{row['name']} — Score: {row['score']}",
                popup=popup,
                icon=folium.Icon(color=color, icon="home", prefix="fa"),
            ).add_to(m_map)

            for dest_name, route_coords in routes_by_candidate.get(row["name"], {}).items():
                if not route_coords or len(route_coords) < 2:
                    continue
                dest_key = dest_name.lower().replace(" ", "_")
                mins = row.get(f"{dest_key}_walk_min")
                folium.PolyLine(
                    locations=route_coords,
                    color=color,
                    weight=3,
                    opacity=0.6,
                    tooltip=f"{row['name']} → {dest_name}: {mins} min",
                ).add_to(route_group)

        route_group.add_to(m_map)
        folium.LayerControl(collapsed=False).add_to(m_map)

        m_map.save(html_file)
        self._log(f"[+] Saved interactive map to {html_file}")


def main():
    parser = argparse.ArgumentParser(
        description="FlatScorer - Multi-Criteria Apartment Scoring Engine"
    )
    parser.add_argument(
        "-c", "--config", type=str, help="Path to JSON configuration file"
    )
    parser.add_argument(
        "--generate-config", type=str, help="Write default example config JSON to specified file and exit"
    )
    parser.add_argument(
        "--csv", type=str, help="Override output CSV file path"
    )
    parser.add_argument(
        "--html", type=str, help="Override output HTML map file path"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress detailed logs"
    )

    args = parser.parse_args()

    if args.generate_config:
        with open(args.generate_config, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print(f"[+] Generated sample configuration file at '{args.generate_config}'")
        print("    Edit it with your own addresses, then run:")
        print(f"    python {os.path.basename(__file__)} --config {args.generate_config}")
        sys.exit(0)

    if not args.config:
        print("No config file specified. Running with built-in demo data.")
        print(f"To create your own config: python {os.path.basename(__file__)} --generate-config config.json")
        print()

    config = DEFAULT_CONFIG
    if args.config:
        if not os.path.exists(args.config):
            sys.exit(f"Error: Config file '{args.config}' not found.")
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)

    if args.csv:
        config.setdefault("output", {})["csv_file"] = args.csv
    if args.html:
        config.setdefault("output", {})["html_file"] = args.html

    scorer = FlatScorer(config, verbose=not args.quiet)
    scorer.run()


if __name__ == "__main__":
    main()
