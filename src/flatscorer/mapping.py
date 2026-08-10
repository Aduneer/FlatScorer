"""The interactive Folium map: pins, popups and predicted commute routes.

Both HTML surfaces this package produces escape what they interpolate. The map
is a file the user opens locally and a config is meant to be passed around, so a
link out of someone else's config must not be able to close an href.
"""

from __future__ import annotations

import html
from typing import Any

import folium
import pandas as pd

from .config import destination_mode
from .routing import (
    COMMUTE_COLUMN_SUFFIXES,
    DEFAULT_TRAVEL_MODE,
    TRAVEL_MODES,
    commute_column,
)
from .scoring import (
    MAP_COLOUR_BANDS,
    NARROW_MARGIN_THRESHOLD,
    SCORE_SCALE_MAX,
    score_colour,
)


def _listing_link_html(url: str | None) -> str:
    """The map popup's link line, or nothing at all when there is no link.

    Escaped: the popup is raw HTML in a file the user opens locally, and
    config.json is meant to be passed around - so a link out of someone else's
    config must not be able to close the href and open a tag of its own.
    """
    if not url:
        return ""
    return (f'<br><a href="{html.escape(url, quote=True)}" target="_blank" '
            'rel="noopener noreferrer">🔗 View listing</a>')


def generate_map(df: pd.DataFrame, resolved_destinations: dict[str, Any], html_file: str,
                 routes_by_candidate: dict[str, dict[str, list[tuple[float, float]]]] | None = None,
                 *, show_routes: bool = True, log=None):
    """Generate interactive Folium map with candidate apartments, destination pins, and predicted commute routes.

    `log` takes the same one-string callable `FlatScorer._log` is, so the map
    keeps narrating itself when driven by the engine and stays silent when not.
    """
    log = log or (lambda _msg: None)
    routes_by_candidate = routes_by_candidate or {}
    first_lat = df.iloc[0]["lat"]
    first_lon = df.iloc[0]["lon"]
    m_map = folium.Map(location=[first_lat, first_lon], zoom_start=13)

    dest_modes = {name: destination_mode(data["info"]) for name, data in resolved_destinations.items()}
    # An all-walk map keeps the layer name it has always had; only a map that
    # actually mixes modes needs the broader wording.
    layer_name = ("Predicted walking routes" if set(dest_modes.values()) <= {"walk"}
                  else "Predicted commute routes")
    route_group = folium.FeatureGroup(name=layer_name, show=show_routes)

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

    score_spread = float(df["score"].max() - df["score"].min())

    # Pins are coloured by absolute score, not by rank within the set. The old
    # min-max stretch always painted the worst candidate red and the best
    # green - even for a 0.1-point spread, which contradicted the sensitivity
    # report calling the same gap a tie. Now the scale is real, so two flats
    # that score alike simply get the same colour, and a set of mediocre flats
    # is allowed to be uniformly orange.
    log(f"[i] Map pins are coloured by absolute score: green above "
        f"{MAP_COLOUR_BANDS[0][0] * SCORE_SCALE_MAX:.1f}, orange above "
        f"{MAP_COLOUR_BANDS[1][0] * SCORE_SCALE_MAX:.1f}, red below.")
    if len(df) > 1 and score_spread < NARROW_MARGIN_THRESHOLD:
        log(f"[i] All candidates score within {score_spread:.2f} points of each other - "
            "expect the pins to look alike, because they are alike.")

    for _, row in df.iterrows():
        color = score_colour(row["score"])

        dest_lines = []
        for col in df.columns:
            suffix = next((s for s in COMMUTE_COLUMN_SUFFIXES if col.endswith(s)), None)
            if suffix is None:
                continue
            dest_label = col[:-len(suffix)].replace("_", " ").title()
            verb = next(spec["verb"] for spec in TRAVEL_MODES.values() if suffix == f"_{spec['column_suffix']}")
            dest_lines.append(f"{dest_label}: {row[col]} min {verb}")
        dest_html = " | ".join(dest_lines)

        popup = (
            f"<b>{row['name']}</b><br>"
            f"Score: {row['score']} / {SCORE_SCALE_MAX:.0f}<br>"
            f"Rent: €{row['rent_eur']}<br>"
            f"Commute: {dest_html}<br>"
            f"Supermarkets: {row['supermarkets']} | Bakeries: {row['bakeries']}<br>"
            f"Pharmacies: {row['pharmacies']} | Gyms: {row['gyms']}<br>"
            f"Transit stops: {row['transit_stops']}<br>"
            f"Green area nearby: {row['green_area_m2']} m²<br>"
            f"Distance to busy road: {row['dist_busy_road_m']} m"
            # Last line so the click target sits at the bottom of the popup,
            # and absent entirely when the flat carries no link.
            + _listing_link_html(row.get("url"))
        )
        folium.Marker(
            [row["lat"], row["lon"]],
            tooltip=f"{row['name']} — Score: {row['score']} / {SCORE_SCALE_MAX:.0f}",
            popup=popup,
            icon=folium.Icon(color=color, icon="home", prefix="fa"),
        ).add_to(m_map)

        for dest_name, route_coords in routes_by_candidate.get(row["name"], {}).items():
            if not route_coords or len(route_coords) < 2:
                continue
            mode = dest_modes.get(dest_name, DEFAULT_TRAVEL_MODE)
            mins = row.get(commute_column(dest_name, mode))
            folium.PolyLine(
                locations=route_coords,
                color=color,
                weight=3,
                opacity=0.6,
                # Lines are coloured by candidate score, so on a mixed map the
                # dashes are the only thing separating a cycled leg from a
                # walked one.
                dash_array="8" if mode != DEFAULT_TRAVEL_MODE else None,
                tooltip=f"{row['name']} → {dest_name}: {mins} min {TRAVEL_MODES[mode]['verb']}",
            ).add_to(route_group)

    route_group.add_to(m_map)
    folium.LayerControl(collapsed=False).add_to(m_map)

    m_map.save(html_file)
    log(f"[+] Saved interactive map to {html_file}")
