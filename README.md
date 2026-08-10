<p align="center">
  <img src="assets/banner.svg" alt="FlatScorer" width="700"/>
</p>

<p align="center">
  <strong>Score and compare apartments using real OpenStreetMap data.</strong><br>
  Amenities, transit, green space, commute times, rent — all in one weighted score.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#gui">GUI</a> ·
  <a href="#how-it-works">How It Works</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#cli-reference">CLI Reference</a>
</p>

<p align="center">
  <a href="https://github.com/Aduneer/FlatScorer/actions/workflows/ci.yml"><img src="https://github.com/Aduneer/FlatScorer/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Aduneer/FlatScorer" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
</p>

---

## What is this?

FlatScorer is a command-line Python tool that helps you objectively compare
apartments (or any set of candidate addresses) by pulling spatial data from
OpenStreetMap and computing a composite livability score.

You define your candidate flats, the places you care about getting to (office,
university, train station, ...), and how much you value each criterion. The tool
handles the rest — geocoding, network routing, amenity counting — and gives you
a ranked table, a CSV, an interactive HTML map, and a sensitivity analysis that
tells you whether your ranking is robust or hinges on a single weight choice.

**Works anywhere in the world** — CRS is auto-detected from your coordinates.

## Preview

| Terminal output | Interactive map |
|---|---|
| ![Terminal ranking table](assets/screenshot_table.png) | ![Interactive Folium map](assets/screenshot_map.png) |

## Quick Start

Install it as a command-line tool:

```bash
pip install git+https://github.com/Aduneer/FlatScorer.git

# Generate a starter config with demo data
flatscorer --generate-config config.json

# Edit config.json with your own addresses, then run
flatscorer --config config.json
```

[`pipx`](https://pipx.pypa.io/) works too and keeps the fairly heavy geospatial
stack out of your global site-packages: `pipx install "flatscorer[gui] @ git+https://github.com/Aduneer/FlatScorer.git"`.

Or run it straight from a checkout, without installing:

```bash
git clone https://github.com/Aduneer/FlatScorer.git
cd FlatScorer
pip install -r requirements.txt
python FlatScorer.py --generate-config config.json
python FlatScorer.py --config config.json
```

Running without `--config` uses built-in demo data (Washington, DC) so you can
try it immediately.

## GUI

Prefer a form over JSON? There's a Streamlit interface that wraps the same
scoring engine — build your candidate list and destinations in a table,
tune weights with sliders, and get the ranked table and interactive map
right in your browser.

```bash
# Installed as a package — the GUI is an optional extra
pip install "flatscorer[gui] @ git+https://github.com/Aduneer/FlatScorer.git"
flatscorer-gui

# Or from a checkout
pip install -r requirements-gui.txt
streamlit run streamlit_app.py
```

`flatscorer-gui` forwards any extra arguments to `streamlit run`, so
`flatscorer-gui --server.port 8600` does what you'd expect.

It reuses `FlatScorer.py` directly, so results are identical to the CLI —
this is just a friendlier way to build the config and view the output.

A run takes a couple of minutes, most of it waiting on Nominatim's one-request-
per-second geocoding and the OpenStreetMap downloads. The Run page shows a
progress bar naming each step as it starts ("Downloading the cycling street
network..."), so a slow run reads as slow rather than as a hang.

<p align="center">
  <img src="assets/gui_preview.png" alt="FlatScorer Streamlit GUI" width="800"/>
</p>

## How It Works

For each candidate apartment, FlatScorer:

1. **Geocodes** all addresses via Nominatim (through OSMnx).
2. **Downloads** a street network per travel mode your destinations actually use
   (pedestrian, cycling, or both) plus the points of interest for the bounding
   region, from the Overpass API (with automatic mirror failover).
3. **Deduplicates POIs** mapped both as a node and as a building outline
   (see [Duplicate POIs](#duplicate-pois)).
4. **Counts nearby amenities** within a configurable radius (default 500 m):
   supermarkets, bakeries, pharmacies, gyms, bus/tram stops.
5. **Measures green space** — park and forest polygon area plus point features.
6. **Estimates noise exposure** via distance to the nearest primary/secondary road.
7. **Routes commutes** to each of your defined destinations over the real
   network for that destination's travel mode — walking (~5 km/h) or cycling
   (~15 km/h), see [Travel modes](#travel-modes).
8. **Normalizes every metric** onto a common 0–1 scale.
9. **Computes a weighted average** of those normalized values, on a 0–10 scale.

### Travel modes

A destination declares how you get there with `"mode": "walk"` (the default) or
`"mode": "bike"`. It changes the answer substantially: a 35-minute walk is often
a 12-minute cycle, and which flats look good depends on which of those you meant.

```jsonc
"destinations": {
  "Office":     { "address": "...", "weight": 0.20, "mode": "bike" },
  "Supermarket":{ "address": "...", "weight": 0.10 }               // walks, as before
}
```

Each mode routes over **its own** street network, downloaded separately. That is
deliberate and not negotiable: the pedestrian graph carries footways a bike may
not use and drops roads it may, so a cycling time computed on it would be wrong
in both directions at once — and wrong invisibly, since it still produces a
plausible number.

The networks are downloaded lazily, one per mode your destinations actually
mention. An all-walk config — every config written before this existed, and the
shipped example — makes exactly one download, as it always did. Only a genuinely
mixed config pays for a second.

Each mode has its own pace (`walking_speed_m_per_min`, `cycling_speed_m_per_min`)
and its own column in the results: `office_bike_min` beside `supermarket_walk_min`,
so a cycled commute is never reported under a heading that says "walk". On the map,
cycled legs are drawn dashed.

### Duplicate POIs

OpenStreetMap frequently maps one real place twice — a supermarket tagged on a
POI node *and* on the building outline around it. Overpass returns both, so a
naive count sees two supermarkets where a shopper sees one.

This matters more than it sounds. The duplication isn't uniform: it's most
common where mapping is most thorough, which tends to be central, dense,
well-surveyed neighbourhoods. Left alone it hands those areas an amenity bonus
they haven't earned — inflating exactly the places that were already scoring
well, which is the wrong direction for a comparison tool.

FlatScorer merges a node and an area into one feature when the area's geometry
comes within `poi_dedupe_tolerance_m` of the node and their `name` tags don't
disagree (an unnamed building still merges with a named node, which is the usual
shape of the duplicate). For amenity counts the node survives; for green space
the polygon survives, because it carries the m² the green score is made of.

Two nodes are **never** merged with each other. Bus stops legitimately come in
pairs a few meters apart on opposite sides of a road, and collapsing those would
be the same inflation bug pointing the other way.

Every merge is reported in the run log, so the numbers behind a score stay
auditable.

### Reading the score

Every metric is first mapped onto 0–1, then combined as a weighted average and
rescaled to 0–10:

```
score = 10 × Σ (wᵢ × normalizedᵢ) / Σ wᵢ
```

| Term | Normalized as | Anchor (configurable) |
|---|---|---|
| supermarket, bakery, pharmacy, gym, transit | `count / (count + half)` — saturating | `saturation.<metric>` |
| green | same curve over `green_score` (m² / 1000 + 0.5 per point feature) | `saturation.green` |
| noise | `min(distance, cap) / cap` | `noise_cap_m` |
| rent | `1 − rent / budget`, clamped to 0 | `rent_budget_eur` |
| each destination | `1 − minutes / cap`, clamped to 0 | `commute_cap_min` |

**The 0–10 scale is real.** Every term is bounded and the weights are normalized
to sum to 1, so no input can push a score outside 0–10 — and because the anchors
are absolute constants rather than the candidate set's own min and max, a score
means the same thing in every run. Two runs a month apart are comparable.

Three consequences worth knowing:

- **Only the relative sizes of weights matter.** Doubling every slider changes
  nothing. A weight's real meaning is its *share* of the total, which is the most
  points out of 10 that term can contribute — the GUI's Weights page shows this
  share for every factor, destinations included.
- **Destination weights compete in the same pool** as the amenity weights, rather
  than being points-per-minute penalties.
- **Amenities have diminishing returns.** With `saturation.supermarket: 2`, the
  first supermarket within the radius is worth far more than the sixth. This is
  deliberate: a raw count let a well-mapped district out-score everything else on
  supermarkets alone.

The sensitivity report prints the gap between the top two; under 0.25 points they
are reported as effectively tied. Map pins are coloured by absolute score (green
above 6.6, orange above 3.3, red below), so a pin colour also means the same
thing in every run — and a field of similar flats is allowed to look similar.

> **Migrating from a pre-normalization config:** scores from older versions are
> not comparable to these. `euros_per_extra_minute` is gone (rent is no longer
> converted to commute-minutes); set `rent_budget_eur` to the most you would pay
> instead. Old configs still load — the removed key is simply ignored and the new
> anchors fall back to their defaults.

### Output

| Artifact | Description |
|---|---|
| Terminal table | Ranked summary printed to stdout |
| `apartment_scores.csv` | Full metrics for every candidate |
| `apartment_map.html` | Interactive Folium map with color-coded pins and predicted commute routes |
| Sensitivity report | ±20% weight perturbation check on ranking stability |

## Configuration

Everything is driven by a single JSON file. Generate a template with
`--generate-config`, then edit it.

```jsonc
{
  // Apartments to compare
  "candidates": [
    {
      "name": "Flat A - Kreuzberg",
      "address": "Oranienstraße 1, 10999 Berlin, Germany",
      "rent": 1200,
      // Optional. Never scored — carried into the CSV and the map popup so you
      // can click from a result straight back to the listing. Omit it entirely
      // if you have no link; must be http:// or https:// if you do.
      "url": "https://www.example-listings.de/expose/12345"
    }
  ],

  // Places you commute to — each gets a travel-time column
  "destinations": {
    "Office": {
      "address": "Alexanderplatz 1, 10178 Berlin, Germany",
      "weight": 0.20,        // importance, relative to the weights below
      "mode": "bike",        // "walk" (default) or "bike" — see Travel modes
      "icon": "briefcase",   // FontAwesome icon on the map
      "color": "blue"
    }
  },

  // How much each factor matters, relative to the others. Only the ratios
  // count — these are normalized to sum to 100% before scoring.
  "weights": {
    "supermarket": 0.30,
    "bakery":      0.10,
    "pharmacy":    0.15,
    "gym":         0.15,
    "transit":     0.33,
    "green":       0.05,
    "noise":       0.05,
    "rent":        0.25
  },

  "parameters": {
    "buffer_m": 500,               // amenity search radius in meters
    "noise_cap_m": 200,            // quiet term maxes out at this distance from a busy road
    "rent_budget_eur": 2500,       // rent at/above this scores 0 on the rent term
    "commute_cap_min": 45,         // a commute this long scores 0 for that destination
    "walking_speed_m_per_min": 83.33,  // assumed pace; 83.33 m/min = 5 km/h
    "cycling_speed_m_per_min": 250,    // assumed pace; 250 m/min = 15 km/h
    "max_bbox_span_km": 30,        // refuse to download an area wider than this
    "saturation": {                // count earning half credit (diminishing returns)
      "supermarket": 2, "bakery": 2, "pharmacy": 1,
      "gym": 1, "transit": 4, "green": 30
    },
    "projected_crs": "auto",       // auto-detect UTM zone, or e.g. "EPSG:25832"
    "show_walk_routes": true       // draw predicted commute routes on the map by default
  },

  "output": {
    "csv_file": "apartment_scores.csv",
    "html_file": "apartment_map.html"
  }
}
```

### Key parameters explained

- **`rent_budget_eur`** — The rent you consider unaffordable. Rent at or above it
  scores 0 on the rent term, free rent scores 1, and everything in between is
  linear. Set it to the top of your budget; setting it far above your actual
  range flattens the differences between candidates.

- **`commute_cap_min`** — The travel time at which a destination stops earning
  anything. Same shape as the rent term, including the same tradeoff: everything
  past the cap scores 0, so a 50-minute commute and a 90-minute commute are
  indistinguishable on that term. One cap covers every mode — it is how long you
  are willing to travel, not how far. If the score breakdown shows a destination
  at 0.00 for every candidate, raise the cap, switch that destination to `bike`,
  or accept that nobody is getting there. Both anchors are deliberately absolute
  — that is what makes a score mean the same thing between runs.

- **`walking_speed_m_per_min` / `cycling_speed_m_per_min`** — The pace each mode's
  routed distances are divided by to get minutes. The walking default 83.33 m/min
  is 5 km/h, the usual planning figure for an unhurried adult on the flat; 100
  m/min (6 km/h) is a brisk walker. The cycling default 250 m/min is 15 km/h,
  urban cycling *including* junctions, lights and locking up — not the speed a fit
  rider holds on a clear path, which is why it is well under what a bike computer
  reports. Both only mean anything alongside `commute_cap_min`, because they
  multiply out: a slower pace makes every commute longer in minutes and so pushes
  more destinations towards the cap. If you change one, sanity-check the other —
  the routed distances themselves have not moved.

- **`saturation`** — The count that earns half credit for each amenity, i.e. how
  quickly more of something stops helping. Lower is easier to satisfy: at
  `supermarket: 1`, a single supermarket already earns half the term.

- **`poi_dedupe_tolerance_m`** — How close a POI node and a building outline have
  to be before they're treated as one real-world place mapped twice (see
  [Duplicate POIs](#duplicate-pois) below). Distance is measured to the building's
  geometry rather than its centroid, so a node anywhere *inside* the building
  already counts as 0 m and this only has to absorb nodes placed just outside a
  wall. Raise it and genuinely distinct neighbouring shops start merging; 0
  merges only nodes that fall exactly on the outline.

- **`max_bbox_span_km`** — A sanity check on how far apart your addresses are,
  not a scoring anchor. Everything you list has to fit inside a box this wide, or
  the run stops *before* the OpenStreetMap download with a message naming the
  address that is furthest from your candidates. It exists because the usual
  cause of a huge search area is a typo: an address that geocoded to the wrong
  city produces a box hundreds of km across, and asking Overpass for that much
  pedestrian network is a several-minute hang ending in a rejection. 30 km covers
  any single-city search; raise it if you are genuinely comparing flats across a
  region.

- **`projected_crs`** — Coordinate reference system for metric distance
  calculations. `"auto"` picks the correct UTM zone for your region. Override
  with an EPSG code if you have a preference.

- **Destination `weight`** — How much this commute matters relative to every
  other factor. It competes in the same pool as the amenity weights, so a
  destination at 0.15 against weights totalling 1.5 controls 10% of the score —
  at most 1 point out of 10, earned by living next door to it.

- **Destination `mode`** — `"walk"` (the default) or `"bike"`, deciding which
  street network this commute is routed over and which pace it is divided by.
  See [Travel modes](#travel-modes); omitting it walks, so every config written
  before cycling existed scores exactly as it did.

- **`show_walk_routes`** — Whether the map's predicted-routes layer starts
  visible. The routes trace each candidate's actual shortest path to every
  destination over that destination's own network (color-matched to the
  candidate's score, dashed for cycled legs) and can always be toggled via the
  map's layer control regardless of this setting.

### Configuration is validated before anything runs

Both the CLI and the GUI check the whole config up front — before any geocoding
or OpenStreetMap download — and report **every** problem at once, naming the
candidate rather than just the field:

```
$ python FlatScorer.py --config config.json
Error: configuration is not valid (2 problem(s)):
  - candidates[0] ('Flat A'): 'rent' is missing - a flat with no rent would score
    as if it were free, taking full credit on the rent term and likely topping
    the ranking
  - destinations['Work']: 'address' is missing or empty
```

Rent is the one worth calling out. Because the rent term gives full credit at €0
and none at `rent_budget_eur`, a candidate with a blank, zero, or negative rent
doesn't score neutrally — it scores *perfectly* on that term and tends to win.
So a missing or non-positive rent is rejected rather than guessed at. The other
checks cover missing names and addresses, duplicate candidate names (they'd
silently overwrite each other), non-numeric or negative weights, an all-zero
weight vector, non-positive normalization anchors, an unrecognised destination
travel `mode`, and a candidate `url` that isn't `http://` or `https://` — that
one is a scheme check rather than fussiness, since the link is rendered as a
clickable href in the map popup and config files get passed around.

In the GUI the same problems appear on the Run page and the run button stays
disabled until they're fixed.

## CLI Reference

```
usage: FlatScorer.py [-h] [-c CONFIG] [--generate-config FILE]
                       [--csv FILE] [--html FILE] [-q]

options:
  -c, --config PATH            JSON configuration file
  --generate-config PATH       Write a starter config and exit
  --csv PATH                   Override CSV output path
  --html PATH                  Override HTML map output path
  -q, --quiet                  Suppress progress logs
```

## Requirements

- Python 3.9+
- Dependencies: `osmnx`, `networkx`, `geopandas`, `pandas`, `folium`,
  `shapely`, `requests`, plus `scipy` and `scikit-learn` — osmnx treats those
  last two as optional, but nearest-node lookup needs them, so routing fails
  mid-run without them

Install everything with:

```bash
pip install -r requirements.txt
```

## Overpass API Resilience

The Overpass API (used for OSM data) can be flaky. FlatScorer automatically
retries failed requests and rotates through three public mirrors:

- `overpass-api.de`
- `overpass.kumi.systems`
- `overpass.private.coffee`

If all mirrors fail, check your internet connection or try again later — the
OSM infrastructure occasionally has outages.

Geocoding goes through Nominatim, which allows **one request per second** and has
no mirrors. FlatScorer paces its geocoding calls to stay inside that limit and
retries transient failures up to three times; an address Nominatim simply cannot
match is reported immediately rather than retried, and the affected candidate is
listed explicitly as missing from the ranking.

## Development

```bash
pip install -e ".[dev]"     # or: pip install -r requirements-dev.txt
pytest          # offline test suite — no Overpass, no Nominatim
ruff check .
```

Packaging metadata lives in `pyproject.toml`, which also holds the ruff and
pytest config. The runtime dependency list is read from `requirements.txt` so
there's exactly one source of truth; the `gui` and `dev` extras are declared
inline and mirror `requirements-gui.txt` / `requirements-dev.txt`, which stay
because CI installs from them. FlatScorer ships as three top-level modules
(`FlatScorer`, `streamlit_app`, `flatscorer_gui`) rather than a package
directory, so `[tool.setuptools] py-modules` lists them explicitly.

The tests cover the scoring maths (metric normalization, `compute_score`'s 0–10
bounds under extreme inputs, the score breakdown, the sensitivity check), config
validation, POI deduplication, the spatial helpers, geocode throttling/retry, map pin colouring, and
the Streamlit `data_editor` state handling via `streamlit.testing`. Both `pytest`
and `ruff check` gate CI.

A `package` CI job builds the wheel and installs it into a clean venv with no
checkout on `sys.path`, so a module missing from `py-modules` or a broken console
script fails in CI rather than for whoever runs `pip install` first.

`requirements.txt` declares version *ranges*, so CI installing the newest of each
would never exercise the declared floors — and a wrong floor then only breaks for
whoever happens to resolve to it. The `min-versions` CI job installs
`requirements-min.txt` (every `>=` pinned to `==`) on the oldest supported Python
and runs the import, config-generation, and routing checks against it. Move a
floor in `requirements.txt` and you must move it there too.

## Data & Attribution

FlatScorer runs on free, community-maintained OpenStreetMap infrastructure:

- Map, amenity, and road data: © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, [ODbL](https://opendatacommons.org/licenses/odbl/).
- Geocoding via [Nominatim](https://nominatim.org/), subject to its
  [usage policy](https://operations.osmfoundation.org/policies/nominatim/) (max 1
  request/sec, no bulk geocoding). FlatScorer's per-run usage stays well within
  this, but don't script it into a batch job over hundreds of addresses.
- POI and network data via the [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API),
  through the public mirrors listed below, each with their own fair-use rules.
  Be a good citizen — keep bounding boxes to what you actually need.

## License

MIT — see [LICENSE](LICENSE).
