"""`FlatScorer` — the pipeline that drives every other module in order.

Orchestration only. The ordering in `run()` carries most of the design: config
validation and the search-area guard both raise *before* any network call, and
the two Overpass downloads are the only things that ever touch the network.

The scoring maths, the map and the CRS resolution live in `scoring`, `mapping`
and `spatial`; the methods here delegate to them so that `FlatScorer(cfg)` stays
the single object the CLI and the GUI both drive.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import osmnx as ox
import pandas as pd

from . import geocode, mapping, osm, paths, report, scoring, spatial
from .config import (
    ConfigError,
    _as_number,
    candidate_image,
    candidate_url,
    destination_mode,
    validate_config,
)
from .osm import DEFAULT_POI_DEDUPE_TOLERANCE_M
from .routing import (
    DEFAULT_CYCLING_SPEED_M_PER_MIN,
    DEFAULT_DETOUR_FACTOR,
    DEFAULT_ROUTING_MODE,
    DEFAULT_TRAVEL_MODE,
    DEFAULT_WALKING_SPEED_M_PER_MIN,
    TRAVEL_MODES,
    commute_column,
    nearest_node,
    route_time,
    straight_line_time,
)
from .scoring import (
    DEFAULT_COMMUTE_CAP_MIN,
    DEFAULT_RENT_BUDGET_EUR,
    DEFAULT_SATURATION,
    NARROW_MARGIN_THRESHOLD,
    SCORE_SCALE_MAX,
    _winner_margin,
)
from .spatial import (
    DEFAULT_MAX_BBOX_SPAN_KM,
    check_search_area,
    count_nearby,
    green_area_and_points,
    nearest_distance_m,
)

# Roughly how long each pipeline step takes relative to the others, used *only*
# to place a caller's progress bar. They are wall-clock ratios, not step counts,
# because the steps differ by more than an order of magnitude: a geocode is one
# rate-limited second while a street-network download is tens of them. Counting
# steps equally would park the bar at 20% for most of the run, which is barely
# better than the spinner it replaces. They do not have to be exact - the bar
# only has to keep moving and finish where it says it will.
PROGRESS_WEIGHTS = {
    "geocode": 1.0,   # per address
    "graph": 25.0,    # per travel mode; the single slowest thing here
    "pois": 20.0,
    "score": 2.0,     # per candidate
    "output": 1.0,    # CSV, sensitivity report, map and overview report together
}


class FlatScorer:
    """Multi-criteria apartment scoring engine."""

    def __init__(self, config: dict[str, Any], verbose: bool = True,
                 progress: Any = None):
        self.config = config
        self.verbose = verbose

        # Optional `callable(fraction, label)` invoked as run() moves through the
        # pipeline: `fraction` is 0..1 of the work finished, `label` names the
        # step now starting. The GUI drives a progress bar with it; the CLI
        # passes nothing and every call below becomes a no-op.
        self.progress = progress
        self._progress_total = 0.0
        self._progress_done = 0.0
        self._progress_pending = 0.0

        self.candidates_raw = config.get("candidates", [])
        self.destinations_config = config.get("destinations", {})
        self.weights = config.get("weights", {})
        self.params = config.get("parameters", {})
        self.output_config = config.get("output", {})

        self.buffer_m = self.params.get("buffer_m", 500)
        self.noise_cap_m = self.params.get("noise_cap_m", 200)
        self.rent_budget_eur = self.params.get("rent_budget_eur", DEFAULT_RENT_BUDGET_EUR)
        self.commute_cap_min = self.params.get("commute_cap_min", DEFAULT_COMMUTE_CAP_MIN)
        self.poi_dedupe_tolerance_m = self.params.get("poi_dedupe_tolerance_m", DEFAULT_POI_DEDUPE_TOLERANCE_M)
        self.max_bbox_span_km = self.params.get("max_bbox_span_km", DEFAULT_MAX_BBOX_SPAN_KM)
        # Named to match TRAVEL_MODES[...]["speed_param"], which is how
        # `mode_speed()` finds the right one without a lookup table of its own.
        self.walking_speed_m_per_min = self.params.get("walking_speed_m_per_min", DEFAULT_WALKING_SPEED_M_PER_MIN)
        self.cycling_speed_m_per_min = self.params.get("cycling_speed_m_per_min", DEFAULT_CYCLING_SPEED_M_PER_MIN)
        # Per-metric half-credit points; a config may override any subset.
        self.saturation = dict(DEFAULT_SATURATION, **self.params.get("saturation", {}))
        self.configured_crs = self.params.get("projected_crs", "auto")
        self.show_walk_routes = self.params.get("show_walk_routes", True)
        # "network" routes over a downloaded street network; "straight_line"
        # skips that download entirely. See ROUTING_MODES for the bargain.
        self.routing_mode = self.params.get("routing_mode", DEFAULT_ROUTING_MODE)
        self.detour_factor = self.params.get("detour_factor", DEFAULT_DETOUR_FACTOR)

        # Populated by run(): (name, address) pairs that failed to geocode and
        # were therefore dropped from the ranking. Callers (e.g. the GUI) read
        # this to surface silent losses instead of quietly ranking fewer flats.
        self.failed_candidates: list[tuple[str, str]] = []
        self.failed_destinations: list[tuple[str, str]] = []

    def _log(self, msg: str):
        if self.verbose:
            print(msg)

    def _plan_progress(self) -> float:
        """Total weight of the run about to start, from the config alone.

        Everything here is known before the first network call, which is the
        point: a bar whose maximum is discovered halfway through is a bar that
        jumps backwards. Geocoding failures make the plan an *over*-estimate
        (fewer candidates to score), so `_progress_finish` pins the end at 1.0
        rather than letting the bar stop short.
        """
        destinations = [d for d in self.destinations_config.values() if isinstance(d, dict)]
        # No graph is downloaded in straight-line mode, so budgeting for one
        # would park the bar at the step that no longer exists - and the graph
        # download is by far the heaviest weight in the table.
        modes = set() if self.routing_mode == "straight_line" else {destination_mode(d) for d in destinations}
        return (PROGRESS_WEIGHTS["geocode"] * (len(self.candidates_raw) + len(destinations))
                + PROGRESS_WEIGHTS["graph"] * len(modes)
                + PROGRESS_WEIGHTS["pois"]
                + PROGRESS_WEIGHTS["score"] * len(self.candidates_raw)
                + PROGRESS_WEIGHTS["output"])

    def _progress_step(self, label: str, weight: float = 0.0):
        """Announce the step about to run, banking the weight of the previous one.

        The fraction describes what has *finished* while the label describes what
        is *starting*, which is the pairing a progress bar wants: someone reading
        "Downloading the cycling street network..." at 35% is being told where the
        wait is, not where it was. Carrying the previous step's weight here is
        what lets each call name one thing and cost one thing.
        """
        if self.progress is None:
            return
        self._progress_done += self._progress_pending
        self._progress_pending = weight
        fraction = min(self._progress_done / self._progress_total, 1.0) if self._progress_total > 0 else 1.0
        self.progress(fraction, label)

    def _progress_finish(self, label: str = "Finished"):
        """Pin the bar at 1.0, whatever the plan estimated."""
        if self.progress is not None:
            self.progress(1.0, label)

    def mode_speed(self, mode: str) -> float:
        """The configured pace, in m/min, for one travel mode."""
        spec = TRAVEL_MODES.get(mode, TRAVEL_MODES[DEFAULT_TRAVEL_MODE])
        return float(getattr(self, spec["speed_param"]))

    @property
    def anchors(self) -> scoring.Anchors:
        """This run's absolute normalization anchors, as the scoring maths wants them."""
        return scoring.Anchors(
            saturation=self.saturation,
            noise_cap_m=self.noise_cap_m,
            rent_budget_eur=self.rent_budget_eur,
            commute_cap_min=self.commute_cap_min,
            destinations=self.destinations_config,
        )

    def resolve_crs(self, lats: list[float], lons: list[float]) -> str:
        """Determine projected CRS (e.g. UTM zone) for metric calculations."""
        crs = spatial.resolve_crs(lats, lons, self.configured_crs)
        if crs != self.configured_crs:
            self._log(f"[+] Auto-detected optimal projected CRS: {crs}")
        return crs

    # The scoring maths proper lives in `scoring`, as pure functions over an
    # `Anchors` bundle, so it can be read and tested without an engine instance.
    # These four stay as methods because that is how the CLI, the GUI and the
    # sensitivity check have always reached them.

    def normalize_metrics(self, m: dict[str, Any]) -> dict[str, float]:
        """Put every raw metric on a common 0..1 scale, keyed by its weight name."""
        return scoring.normalize_metrics(m, self.anchors)

    def _resolve_weight(self, key: str, weights: dict[str, float]) -> float:
        """Look up a term's weight, falling back through the config to the defaults."""
        return scoring.resolve_weight(key, weights, self.anchors)

    def score_breakdown(self, m: dict[str, Any], weights: dict[str, float]) -> dict[str, dict[str, float]]:
        """Per-term weight, influence share, normalized value and points contributed."""
        return scoring.score_breakdown(m, weights, self.anchors)

    def compute_score(self, m: dict[str, Any], weights: dict[str, float]) -> float:
        """Score a candidate on a fixed 0..SCORE_SCALE_MAX scale."""
        return scoring.compute_score(m, weights, self.anchors)


    def log_score_breakdown(self, breakdowns: dict[str, dict[str, dict[str, float]]]):
        """Print each term's influence share and what it contributed to every candidate."""
        if not breakdowns:
            return
        first = next(iter(breakdowns.values()))
        table = pd.DataFrame(
            {"share": {k: f"{v['share'] * 100:.1f}%" for k, v in first.items()}}
            | {
                name: {k: round(v["contribution"], 2) for k, v in bd.items()}
                for name, bd in breakdowns.items()
            }
        )
        table.loc["TOTAL"] = ["100.0%"] + [
            round(sum(v["contribution"] for v in bd.values()), 2) for bd in breakdowns.values()
        ]
        self._log(f"\nScore breakdown (points out of {SCORE_SCALE_MAX:.0f}, "
                  "'share' is each weight's influence after normalization):")
        self._log(table.to_string())

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
                self._log(f"[!] That is under {NARROW_MARGIN_THRESHOLD:.2f} points on a "
                          f"0-{SCORE_SCALE_MAX:.0f} scale - the top two are effectively tied; "
                          "treat the ranking as a toss-up rather than a clear winner.")

    def run(self) -> pd.DataFrame:
        """Execute geocoding, spatial network fetching, metric calculation, and reporting.

        Raises ConfigError before any network call if the config can't be scored.
        """
        problems = validate_config(self.config)
        if problems:
            raise ConfigError(problems)

        self._progress_total = self._plan_progress()
        self._progress_done = 0.0
        self._progress_pending = 0.0

        self._log("Geocoding candidate apartment addresses...")
        self.failed_candidates = []
        self.failed_destinations = []
        resolved_candidates = {}
        for candidate in self.candidates_raw:
            name = candidate["name"]
            addr = candidate["address"]
            self._progress_step(f"Geocoding candidate '{name}'...", PROGRESS_WEIGHTS["geocode"])
            # validate_config has already guaranteed this parses to a positive
            # number; coerce so a config written as "1800" still scores as 1800.
            rent = _as_number(candidate["rent"])
            coords = geocode.geocode_safe(addr, name)
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
            self._progress_step(f"Geocoding destination '{dest_name}'...", PROGRESS_WEIGHTS["geocode"])
            coords = geocode.geocode_safe(addr, dest_name)
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

        # Refuse an implausibly large box *before* the download rather than after
        # it: a single address geocoded to the wrong city is otherwise a multi-
        # minute hang ending in an Overpass rejection with nothing to act on.
        # Labels carry the address so the message points at the entry to fix.
        candidate_points = {
            f"candidate '{name}' ({info['address']})": info["coords"]
            for name, info in resolved_candidates.items()
        }
        area_points = dict(candidate_points, **{
            f"destination '{dest_name}' ({data['info']['address']})": data["coords"]
            for dest_name, data in resolved_destinations.items()
        })
        span_km = check_search_area(area_points, projected_crs, self.max_bbox_span_km,
                                    centre_labels=candidate_points)
        self._log(f"[+] Search area spans {span_km:.1f} km (limit {self.max_bbox_span_km:g} km)")

        # One street network per travel mode actually used, in the order the
        # destinations first mention them. Downloading lazily is what keeps an
        # all-walk config - every config that predates cycling, including the
        # shipped example - paying for exactly the one download it always paid
        # for; only a genuinely mixed config pays for a second.
        dest_modes = {
            dest_name: destination_mode(data["info"])
            for dest_name, data in resolved_destinations.items()
        }
        modes_in_use = list(dict.fromkeys(dest_modes.values()))

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

        graphs = {}
        approximate = self.routing_mode == "straight_line"
        if approximate:
            # The whole point of the mode: the street network is ~80% of a run's
            # wall clock and ~21 MB of Overpass traffic, and it is being skipped,
            # not deferred. Say so plainly - a user comparing two runs needs to
            # know which one measured and which one estimated.
            if modes_in_use:
                self._log(f"\nEstimating commutes from straight-line distance x {self.detour_factor:g} "
                          "- no street network will be downloaded.")
                self._log("    Commute times are approximate. Ranking is barely affected by this, "
                          "but the minutes themselves carry a few percent of error.")
        else:
            for mode in modes_in_use:
                spec = TRAVEL_MODES[mode]
                self._log(f"\nDownloading the {spec['label']} street network from OpenStreetMap...")
                self._progress_step(f"Downloading the {spec['label']} street network from OpenStreetMap "
                                    "(the slowest step - a minute is normal)...", PROGRESS_WEIGHTS["graph"])
                # network_type is bound as a default rather than closed over, so
                # the lambda can't be caught out by the loop variable moving on.
                graphs[mode] = osm.query_with_retry(
                    lambda network_type=spec["network_type"]: ox.graph_from_bbox(bbox=bbox, network_type=network_type)
                )
        if not modes_in_use:
            self._log("\nNo destinations to route to, so no street network is needed.")

        self._log("Downloading points of interest (POIs) from OpenStreetMap...")
        self._progress_step("Downloading shops, transit stops and green space from OpenStreetMap...",
                            PROGRESS_WEIGHTS["pois"])
        pois = osm.query_with_retry(get_pois)

        supermarkets = osm.safe_filter(pois, "shop", "supermarket")
        bakeries     = osm.safe_filter(pois, "shop", "bakery")
        pharmacies   = osm.safe_filter(pois, "amenity", "pharmacy")
        gyms         = osm.safe_filter(pois, "leisure", "fitness_centre")
        parks        = osm.safe_filter(pois, "leisure", "park")
        greenland    = osm.safe_filter(pois, "landuse", ["grass", "forest"])
        green_all    = pd.concat([gdf for gdf in [parks, greenland] if not gdf.empty]) if not (parks.empty and greenland.empty) else gpd.GeoDataFrame()
        bus          = osm.safe_filter(pois, "highway", "bus_stop")
        tram         = osm.safe_filter(pois, "railway", "tram_stop")
        transit      = pd.concat([gdf for gdf in [bus, tram] if not gdf.empty]) if not (bus.empty and tram.empty) else gpd.GeoDataFrame()
        busy_roads   = osm.safe_filter(pois, "highway", ["primary", "secondary"])

        def proj(gdf):
            return gdf.to_crs(projected_crs) if (gdf is not None and len(gdf) > 0) else gdf

        supermarkets_p, bakeries_p, pharmacies_p, gyms_p, transit_p, green_p, roads_p = map(
            proj, [supermarkets, bakeries, pharmacies, gyms, transit, green_all, busy_roads]
        )

        def dedupe(gdf, label, keep="points"):
            """Collapse node+area duplicates, reporting what it removed."""
            before = len(gdf) if gdf is not None else 0
            result = osm.dedupe_features(gdf, self.poi_dedupe_tolerance_m, keep=keep)
            removed = before - (len(result) if result is not None else 0)
            if removed:
                self._log(f"    {label}: merged {removed} of {before} feature(s) mapped as both a node and an area")
            return result

        # Roads are excluded on purpose: they only feed nearest_distance_m, which
        # takes a minimum, so a duplicated road can't inflate anything.
        self._log("\nDeduplicating POIs mapped as both a node and an area...")
        supermarkets_p = dedupe(supermarkets_p, "supermarkets")
        bakeries_p     = dedupe(bakeries_p, "bakeries")
        pharmacies_p   = dedupe(pharmacies_p, "pharmacies")
        gyms_p         = dedupe(gyms_p, "gyms")
        transit_p      = dedupe(transit_p, "transit stops")
        # Green keeps the polygon instead: it carries the m² that the green score
        # is mostly made of, while the node would only add a 0.5 bonus.
        green_p        = dedupe(green_p, "green spaces", keep="areas")

        # Each destination's nearest graph node is the same for every candidate,
        # so resolve it once here instead of once per (candidate, destination).
        # Keyed by (mode, destination), never by destination alone: a node id is
        # only meaningful in the graph it came from, and the same id names a
        # different junction in the cycling network. Reusing one across modes
        # produces a plausible commute time rather than an error, so nothing
        # downstream would notice.
        # There are no graphs to look nodes up in when the commute is estimated.
        dest_nodes = {} if approximate else {
            (dest_modes[dest_name], dest_name): nearest_node(graphs[dest_modes[dest_name]], data["coords"])
            for dest_name, data in resolved_destinations.items()
        }

        # Worth stating: these convert every routed distance into the minutes that
        # commute_cap_min judges, so a reader comparing two runs needs to know them.
        paces = ", ".join(
            f"{TRAVEL_MODES[mode]['label']} at {self.mode_speed(mode):g} m/min "
            f"= {self.mode_speed(mode) * 60 / 1000:.1f} km/h"
            for mode in modes_in_use
        )
        self._log(f"\nScoring candidates ({paces})..." if paces else "\nScoring candidates...")
        metrics_by_name = {}
        routes_by_candidate = {}
        rows = []

        # The listing-link column appears only when something actually carries a
        # link, so a config predating the field exports exactly the CSV it always
        # did. Decided up front rather than by dropping an all-blank column after
        # the fact, which would depend on the column's dtype surviving the frame.
        any_listing_url = any(candidate_url(info["raw"]) for info in resolved_candidates.values())

        for name, info in resolved_candidates.items():
            self._progress_step(f"Scoring '{name}'...", PROGRESS_WEIGHTS["score"])
            lat, lon = info["coords"]
            rent = info["rent"]
            # Per mode for the same reason the destination cache is: this flat's
            # nearest walking junction and nearest cycling junction are different
            # nodes in different graphs.
            orig_nodes = {} if approximate else {mode: nearest_node(graphs[mode], (lat, lon)) for mode in modes_in_use}

            green_area_m2, green_points = green_area_and_points(lat, lon, green_p, projected_crs, dist=self.buffer_m)
            dist_to_busy_road = nearest_distance_m(lat, lon, roads_p, projected_crs)
            effective_road_dist = dist_to_busy_road if dist_to_busy_road is not None else self.noise_cap_m

            dest_times = {}
            dest_routes = {}
            for dest_name, dest_data in resolved_destinations.items():
                dest_coords = dest_data["coords"]
                mode = dest_modes[dest_name]
                if approximate:
                    # No route is recorded, so none is drawn. A straight segment
                    # between two points is not a walkable path, and putting one
                    # on the map would claim it was.
                    dest_times[dest_name] = straight_line_time(
                        (lat, lon), dest_coords,
                        speed_m_per_min=self.mode_speed(mode),
                        detour_factor=self.detour_factor,
                        projected_crs=projected_crs,
                    )
                else:
                    dest_times[dest_name], dest_routes[dest_name] = route_time(
                        graphs[mode], (lat, lon), dest_coords,
                        speed_m_per_min=self.mode_speed(mode),
                        projected_crs=projected_crs,
                        orig_node=orig_nodes[mode], dest_node=dest_nodes[(mode, dest_name)],
                    )
            routes_by_candidate[name] = dest_routes

            m = {
                "supermarket_count": count_nearby(lat, lon, supermarkets_p, projected_crs, dist=self.buffer_m),
                "bakery_count":      count_nearby(lat, lon, bakeries_p, projected_crs, dist=self.buffer_m),
                "pharmacy_count":    count_nearby(lat, lon, pharmacies_p, projected_crs, dist=self.buffer_m),
                "gym_count":         count_nearby(lat, lon, gyms_p, projected_crs, dist=self.buffer_m),
                "transit_count":     count_nearby(lat, lon, transit_p, projected_crs, dist=self.buffer_m),
                "green_score":       green_area_m2 / 1000.0 + green_points * 0.5,
                "noise_distance_m":  effective_road_dist,
                "destinations_min":  dest_times,
                "rent_eur":          rent,
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
                row[commute_column(dest_name, dest_modes[dest_name], self.routing_mode)] = round(mins, 1)

            if any_listing_url:
                row["url"] = candidate_url(info["raw"]) or ""

            row["lat"] = lat
            row["lon"] = lon
            rows.append(row)

        self._progress_step("Ranking, checking weight sensitivity and building the map and report...",
                            PROGRESS_WEIGHTS["output"])

        df = pd.DataFrame(rows).sort_values("score", ascending=False)
        self._log(f"\nScores are on a fixed 0-{SCORE_SCALE_MAX:.0f} scale and are comparable across runs.")
        self._log(df.drop(columns=["lat", "lon"]).to_string(index=False))

        # Computed once and used twice: the log table and the overview report are
        # two renderings of the same numbers and must not be able to disagree.
        breakdowns = {n: self.score_breakdown(m, self.weights) for n, m in metrics_by_name.items()}
        self.log_score_breakdown(breakdowns)

        csv_file = paths.ensure_parent(self.output_config.get("csv_file", paths.output_path("apartment_scores.csv")))
        # A leading `#` comment carries the ODbL credit with the file itself.
        # `pandas.read_csv(..., comment="#")` skips it and every spreadsheet shows
        # it as a harmless first row - a cost worth paying so a CSV forwarded on
        # its own still says where the data came from.
        with open(csv_file, "w", encoding="utf-8", newline="") as handle:
            handle.write(f"# {osm.OSM_ATTRIBUTION_TEXT}\n")
            df.to_csv(handle, index=False)
        self._log(f"\n[+] Saved score table to {csv_file}")

        self.run_sensitivity_check(metrics_by_name)

        # Generate Folium Map
        html_file = paths.ensure_parent(self.output_config.get("html_file", paths.output_path("apartment_map.html")))
        self.generate_map(df, resolved_destinations, html_file, routes_by_candidate)

        # `image` never enters the frame: a local path is meaningless in a CSV
        # sent to someone else, and the map popup has no use for it.
        images = {}
        for candidate in self.candidates_raw:
            image = candidate_image(candidate)
            if image:
                images[candidate["name"]] = image

        overview_file = paths.ensure_parent(
            self.output_config.get("overview_file", paths.output_path("apartment_overview.html")))
        self.generate_report(df, breakdowns, resolved_destinations, overview_file, images)

        self._progress_finish()
        return df

    def generate_map(self, df: pd.DataFrame, resolved_destinations: dict[str, Any], html_file: str, routes_by_candidate: dict[str, dict[str, list[tuple[float, float]]]] | None = None):
        """Generate interactive Folium map with candidate apartments, destination pins, and predicted commute routes."""
        mapping.generate_map(df, resolved_destinations, html_file, routes_by_candidate,
                             show_routes=self.show_walk_routes, log=self._log)

    def generate_report(self, df: pd.DataFrame,
                        breakdowns: dict[str, dict[str, dict[str, float]]],
                        resolved_destinations: dict[str, Any], html_file: str,
                        images: dict[str, str]):
        """Write the overview report: one card per flat, with its score breakdown."""
        report.generate_report(df, breakdowns, resolved_destinations, html_file, images,
                               failed_candidates=self.failed_candidates, log=self._log)
