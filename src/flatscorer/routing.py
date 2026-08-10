"""Travel modes and the routing over each mode's own network.

Adding a travel mode is a `TRAVEL_MODES` entry, not a branch: that table holds
the network type, the speed parameter, the CSV column suffix and the wording for
logs and popups. `route_time` takes a graph and a speed and knows nothing about
modes.
"""

from __future__ import annotations

import networkx as nx
import osmnx as ox

from .spatial import straight_line_distance_m

# Assumed walking pace, in metres per minute: 83.33 is 5 km/h, the usual planning
# figure for an unhurried adult on the flat. It converts every routed distance
# into the minutes that `commute_cap_min` is measured against, so the two anchors
# only mean what they say together - lower this and every commute term drops.
DEFAULT_WALKING_SPEED_M_PER_MIN = 83.33


# Assumed cycling pace, in metres per minute: 250 is 15 km/h, the usual planning
# figure for urban cycling *including* junctions, lights and locking up - not the
# speed a fit rider holds on a clear path. Same relationship to `commute_cap_min`
# as the walking pace above.
DEFAULT_CYCLING_SPEED_M_PER_MIN = 250.0


# Travel modes a destination may declare via `"mode"`. Each entry names
# everything that is mode-specific about routing a commute, so adding a mode is a
# table entry rather than a branch in `run()`:
#   network_type   - the OSMnx street network to download for it
#   speed_param    - the `parameters` key holding its pace, which is also the
#                    FlatScorer attribute the pace is read back from
#   column_suffix  - what the destination's minutes column is called, so a
#                    cycling commute is never reported in a column saying "walk"
#   label/verb     - wording for the run log and the map popups
#
# The networks genuinely differ - the walk graph carries footways bikes may not
# use and drops roads they may - so a mode always routes over its own graph. A
# bike time computed on the walk network would be wrong in both directions at
# once, and wrong invisibly.
TRAVEL_MODES = {
    "walk": {
        "network_type": "walk",
        "speed_param": "walking_speed_m_per_min",
        "column_suffix": "walk_min",
        "label": "walking",
        "verb": "walk",
    },
    "bike": {
        "network_type": "bike",
        "speed_param": "cycling_speed_m_per_min",
        "column_suffix": "bike_min",
        "label": "cycling",
        "verb": "cycle",
    },
}


# What a destination that doesn't declare a mode gets. Every config written
# before cycling existed is an all-walk config, and has to keep scoring
# identically - including paying for exactly one street-network download.
DEFAULT_TRAVEL_MODE = "walk"


def commute_column(dest_name: str, mode: str = DEFAULT_TRAVEL_MODE) -> str:
    """Name of the table/CSV column carrying a destination's commute minutes.

    The mode is part of the name, so a cycling commute is never reported in a
    column called `..._walk_min`. An all-walk config keeps exactly the columns it
    had before cycling existed.
    """
    suffix = TRAVEL_MODES.get(mode, TRAVEL_MODES[DEFAULT_TRAVEL_MODE])["column_suffix"]
    return f"{dest_name.lower().replace(' ', '_')}_{suffix}"


# Every suffix `commute_column` can produce, longest first so a shorter suffix
# can't strip a prefix of a longer one when a label is recovered from a column.
COMMUTE_COLUMN_SUFFIXES = tuple(
    sorted((f"_{spec['column_suffix']}" for spec in TRAVEL_MODES.values()), key=len, reverse=True)
)


def nearest_node(G: nx.MultiDiGraph, point: tuple[float, float]):
    """Graph node nearest to a (lat, lon) pair.

    A thin wrapper so callers can hoist the lookup out of a loop without
    repeating osmnx's (x, y) argument order, which is the reverse of ours.
    """
    return ox.distance.nearest_nodes(G, point[1], point[0])


def route_time(G: nx.MultiDiGraph, orig: tuple[float, float], dest: tuple[float, float], speed_m_per_min: float = DEFAULT_WALKING_SPEED_M_PER_MIN, projected_crs: str | None = None,
               orig_node=None, dest_node=None) -> tuple[float, list[tuple[float, float]]]:
    """Calculate travel time in minutes and the shortest-path route (list of lat/lon points) between two coordinates over OSM graph G.

    Nothing here is mode-specific: the network to route over and the pace to
    divide by both arrive as arguments, so the same function serves walking and
    cycling. It is the caller's job to pass a graph and a speed that agree with
    each other - a cycling pace over the pedestrian network is not a bike time.

    The speed defaults to `DEFAULT_WALKING_SPEED_M_PER_MIN` so the function stays
    usable standalone, but `run()` always passes the pace configured for the
    destination's mode - the default is a fallback, not the value the tool
    actually scores with.

    `orig_node`/`dest_node` let a caller supply an already-resolved graph node.
    `run()` does, because the endpoints repeat: without it the lookup runs
    2*candidates*destinations times where candidates+destinations would do, and
    on a city-sized graph that lookup is not cheap. Passing them is purely an
    optimisation - omit them and the same nodes are resolved here. They must come
    from *this* graph: a node id resolved against another mode's network names a
    different place, which yields a plausible number rather than an error.
    """
    if orig_node is None:
        orig_node = nearest_node(G, orig)
    if dest_node is None:
        dest_node = nearest_node(G, dest)
    try:
        path = nx.shortest_path(G, orig_node, dest_node, weight="length")
        length_m = nx.path_weight(G, path, weight="length")
        route = [(G.nodes[n]["y"], G.nodes[n]["x"]) for n in path]
        return length_m / speed_m_per_min, route
    except nx.NetworkXNoPath:
        print(f"[!] Warning: No route found between {orig} and {dest}. Defaulting to straight-line distance.")
        dist_m = straight_line_distance_m(orig, dest, projected_crs)
        return dist_m / speed_m_per_min, [orig, dest]
