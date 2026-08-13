<p align="center">
  <img src="https://raw.githubusercontent.com/Aduneer/FlatScorer/main/src/flatscorer/gui/assets/banner.svg" alt="FlatScorer" width="700"/>
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
  <a href="#cli-reference">CLI Reference</a> ·
  <a href="docs/DESIGN.md">Design Notes</a>
</p>

<p align="center">
  <a href="https://pypi.org/project/flatscorer/"><img src="https://img.shields.io/pypi/v/flatscorer" alt="PyPI version"></a>
  <a href="https://github.com/Aduneer/FlatScorer/actions/workflows/ci.yml"><img src="https://github.com/Aduneer/FlatScorer/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/Aduneer/FlatScorer" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
</p>

---

## What is this?

FlatScorer is a Python tool — a command-line one, with an optional
[browser GUI](#gui) over the same engine — that helps you objectively compare
apartments (or any set of candidate addresses) by pulling spatial data from
OpenStreetMap and computing a composite livability score.

You define your candidate flats, the places you care about getting to (office,
university, train station, ...), and how much you value each criterion. The tool
handles the rest — geocoding, network routing, amenity counting — and gives you
a ranked table, a CSV, an interactive HTML map, and a sensitivity analysis that
tells you whether your ranking is robust or hinges on a single weight choice.

**Works anywhere in the world** — CRS is auto-detected from your coordinates.

On a real shortlist, the ranking reproduces the author's own hand-ranking of the
top 3 in roughly 9 cases out of 10 — a small, self-assessed sample, but it is
the check the whole design exists to pass.

📐 **[Design notes](docs/DESIGN.md)** — why the score is built the way it is,
including a four-city measurement of whether the street-network download is
necessary (ρ=0.986 without it) and a POI data source that was evaluated and
rejected.

## Preview

| Terminal output | Interactive map |
|---|---|
| ![Terminal ranking table](https://raw.githubusercontent.com/Aduneer/FlatScorer/main/assets/screenshot_table.png) | ![Interactive Folium map](https://raw.githubusercontent.com/Aduneer/FlatScorer/main/assets/screenshot_map.png) |

## Quick Start

Install it as a command-line tool:

```bash
pip install flatscorer

# Generate a starter config with demo data
flatscorer --generate-config config.json

# Edit config.json with your own addresses, then run
flatscorer --config config.json
```

**No Python toolchain? Run the GUI without installing anything**, using
[`uv`](https://docs.astral.sh/uv/):

```bash
uvx --from "flatscorer[gui]" flatscorer-gui
```

[`pipx`](https://pipx.pypa.io/) works too and keeps the fairly heavy geospatial
stack out of your global site-packages: `pipx install "flatscorer[gui]"`.

Or work from a checkout:

```bash
git clone https://github.com/Aduneer/FlatScorer.git
cd FlatScorer
pip install -e .                      # editable — picks up your edits
flatscorer --generate-config config.json
flatscorer --config config.json
```

The code lives in `src/flatscorer/`, so a bare `pip install -r requirements.txt`
installs the dependencies but not the package itself and `python -m flatscorer`
won't resolve. Either install it as above, or point Python at the source tree for
a one-off: `PYTHONPATH=src python -m flatscorer --config config.json`.

Running without `--config` uses built-in demo data (three flats in central
Berlin) so you can try it immediately.

## GUI

Prefer a form over JSON? There's a Streamlit interface that wraps the same
scoring engine — build your candidate list and destinations in a table,
tune weights with sliders, and get the ranked table and interactive map
right in your browser.

```bash
# The GUI is an optional extra on the published package
pip install "flatscorer[gui]"
flatscorer-gui

# Or from a checkout
pip install -r requirements-gui.txt   # dependencies only
streamlit run src/flatscorer/gui/app.py
```

`streamlit run` works from a checkout without installing the package — `app.py`
puts `src/` on `sys.path` when `flatscorer` isn't importable, because Streamlit
only adds the *script's* own directory.

`flatscorer-gui` forwards any extra arguments to `streamlit run`, so
`flatscorer-gui --server.port 8600` does what you'd expect. To run it without
installing anything at all, see the `uvx` line in [Quick Start](#quick-start).

It reuses the `flatscorer` engine directly, so results are identical to the CLI —
this is just a friendlier way to build the config and view the output.

A run takes a couple of minutes, most of it waiting on Nominatim's one-request-
per-second geocoding and the OpenStreetMap downloads. The Run page shows a
progress bar naming each step as it starts ("Downloading the cycling street
network..."), so a slow run reads as slow rather than as a hang.

<p align="center">
  <img src="https://raw.githubusercontent.com/Aduneer/FlatScorer/main/assets/gui_preview.png" alt="FlatScorer Streamlit GUI" width="800"/>
</p>

<sub>The demo config ships no flat photos, so each card shows the score dial that
stands in for one. Point a candidate's `image` at a URL or a local file to get a
photo panel there instead.</sub>

## How It Works

For each candidate apartment, FlatScorer:

1. **Geocodes** all addresses via Nominatim (through OSMnx).
2. **Downloads** a street network per travel mode your destinations actually use
   (pedestrian, cycling, or both) plus the points of interest for the bounding
   region, from the Overpass API (with automatic mirror failover).
3. **Deduplicates POIs** mapped both as a node and as a building outline
   (see [Duplicate POIs](#duplicate-pois)).
4. **Counts nearby amenities** within a configurable radius (default 500 m):
   supermarkets, bakeries, pharmacies, gyms, and transit stops
   (see [Transit stops are weighted](#transit-stops-are-weighted)).
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
mention. A single-mode config makes exactly one download — that covers every
config written before this existed, which walked everywhere, and the shipped
demo, which cycles to both of its destinations. Only a genuinely mixed config
pays for a second.

Each mode has its own pace (`walking_speed_m_per_min`, `cycling_speed_m_per_min`)
and its own column in the results: `office_bike_min` beside `supermarket_walk_min`,
so a cycled commute is never reported under a heading that says "walk". On the map,
cycled legs are drawn dashed.

### Transit stops are weighted

A metro station and a once-hourly bus stop are not the same thing, so they don't
count the same. Each stop contributes **bus-stop equivalents**:

| Stop | OSM tag | Worth |
|---|---|---|
| Rail / metro station | `railway=station` | 3.0 |
| Rail halt | `railway=halt` | 2.0 |
| Tram stop | `railway=tram_stop` | 1.5 |
| Bus stop | `highway=bus_stop` | 1.0 |

A bus stop stays at 1.0 deliberately: `saturation.transit` keeps meaning exactly
what it did before, so the change only moves scores where the transit genuinely
is better rather than inflating everyone's.

These are a judgement about service level, not a preference — how much transit
matters *to you* is already `weights.transit`, and a second knob on the same axis
would just be a way to count the same opinion twice.

A stop mapped as both (one pole tagged `highway=bus_stop` *and*
`railway=tram_stop`, which is how interchanges are usually drawn) earns both, at
2.5 — it really does give you both services.

The CSV column is `transit_equiv_stops`, and the run log prints the mix it found.

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
| supermarket, bakery, pharmacy, gym | `count / (count + half)` — saturating | `saturation.<metric>` |
| transit | same curve, over weighted stops rather than a headcount | `saturation.transit` |
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

Results are written into `output/` beside wherever you ran the tool, which is
created if it doesn't exist. Point `output.csv_file` / `output.html_file`
somewhere else if you'd rather — a path into a directory that doesn't exist yet
works, it gets created too.

The OpenStreetMap download cache is separate and lives in your user cache
directory (`~/.cache/FlatScorer` on Linux, `~/Library/Caches/FlatScorer` on
macOS, `%LOCALAPPDATA%\FlatScorer\Cache` on Windows). A downloaded street network
is expensive and identical wherever you ask for it, so it's shared across runs
instead of being rebuilt per working directory.

| Artifact | Description |
|---|---|
| Terminal table | Ranked summary printed to stdout |
| `output/apartment_scores.csv` | Full metrics for every candidate. The first line is an OpenStreetMap credit comment — read it with `pd.read_csv(path, comment="#")` |
| `output/apartment_map.html` | Interactive Folium map with color-coded pins and predicted commute routes |
| `output/apartment_overview.html` | One card per flat showing what each criterion contributed to its score — self-contained, opens offline, sendable |
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
      "url": "https://www.example-listings.de/expose/12345",
      // Optional. Never scored — shown on this flat's card in the overview
      // report, which falls back to a score dial when there is no photo.
      // Either an image URL or a path to a file on this computer; a local file
      // is embedded into the report so it stays one sendable file, as long as
      // it is under 2 MB and is a .jpg/.jpeg/.png/.gif/.webp. Anything that
      // can't be read is logged and falls back to the dial.
      // Photos you point this at stay yours to account for: listing photos are
      // usually the agency's copyright, and embedding one copies it into the
      // report — so treat a report containing them as a private document.
      "image": "https://www.example-listings.de/photos/12345.jpg"
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
    "poi_dedupe_tolerance_m": 10,  // how close a node and a building outline merge
    "max_bbox_span_km": 30,        // refuse to download an area wider than this
    "saturation": {                // count earning half credit (diminishing returns)
      "supermarket": 2, "bakery": 2, "pharmacy": 1,
      "gym": 1, "transit": 4, "green": 30   // transit counts bus-stop equivalents
    },
    "projected_crs": "auto",       // auto-detect UTM zone, or e.g. "EPSG:25832"
    "show_walk_routes": true,      // draw predicted commute routes on the map by default
    "routing_mode": "network",     // "network" routes over real streets; "straight_line" estimates
    "detour_factor": 1.25,         // straight_line only: how much longer a real route is
    "nominatim_url": "https://nominatim.openstreetmap.org/"  // geocoding service
  },

  "output": {
    "csv_file": "output/apartment_scores.csv",
    "html_file": "output/apartment_map.html",
    "overview_file": "output/apartment_overview.html"
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
  [Duplicate POIs](#duplicate-pois) above). Distance is measured to the building's
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
  map's layer control regardless of this setting. Has no effect under
  `"routing_mode": "straight_line"`, which measures no routes to draw.

- **`routing_mode`** — How commutes are measured. `"network"` (the default)
  downloads the street network and routes over it, which is exact and is by far
  the slowest step of a run. `"straight_line"` estimates each commute from
  straight-line distance instead and **skips that download entirely**, turning
  a multi-minute run into a few seconds.

  The approximation was measured before it was offered. Across three cities
  chosen for their barriers — Budapest across the Danube, Berlin across the
  Spree, Lisbon across its hills, 18 candidates each — the top pick never
  changed, and the ranking held at every commute weight tried. Roughly 4% of
  candidate *pairs* swapped, never by more than two places. The reason it works
  is that a detour factor is close to constant within a city, and a constant
  multiplier cannot reorder a ranking.

  What you give up is the minutes themselves: they carry a few percent of error
  (mean ~5%, worst case ~19% in testing). Every surface that shows an estimated
  commute labels it `approx.`, and the CSV column is named `..._min_approx` so
  the caveat survives into a file you send to someone else.

  Good for tuning weights, comparing shortlists quickly, or running without
  waiting. Use `"network"` when you want the commute figure itself to be right.

- **`nominatim_url`** — The geocoding service that turns addresses into
  coordinates. Defaults to the public OpenStreetMap instance; point it at your
  own Nominatim if you run one. It is configurable because Nominatim's usage
  policy requires that an application be able to switch service *without
  shipping a new version* — so the endpoint cannot be hard-coded. FlatScorer
  also throttles itself to the policy's 1 request per second and identifies
  itself by name in the `User-Agent`.

- **`detour_factor`** — Used only by `"straight_line"`: how much longer a real
  route is than the straight line between its endpoints. It does **not** affect
  the ranking — a constant cannot reorder anything — so it only decides whether
  the displayed minutes are honest and which candidates fall past
  `commute_cap_min`.

  Street layout drives this more than city size does. Measured medians:
  Washington DC 1.11 (a grid), Berlin 1.215, Lisbon 1.242, Budapest 1.347
  (organic centres). The default 1.25 is the median of those. If your commute
  figures look consistently long or short against a route you know, this is the
  dial — a grid city wants ~1.1, a medieval core ~1.35. Setting it too high is
  worse than too low, because it pushes candidates past the commute cap that
  had not really reached it.

### Configuration is validated before anything runs

Both the CLI and the GUI check the whole config up front — before any geocoding
or OpenStreetMap download — and report **every** problem at once, naming the
candidate rather than just the field:

```
$ flatscorer --config config.json
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
usage: flatscorer [-h] [-c CONFIG] [--generate-config GENERATE_CONFIG]
                  [--csv CSV] [--html HTML] [--check-config] [-q] [--version]

FlatScorer - Multi-Criteria Apartment Scoring Engine

options:
  -h, --help            show this help message and exit
  -c, --config CONFIG   Path to JSON configuration file
  --generate-config GENERATE_CONFIG
                        Write default example config JSON to specified file
                        and exit
  --csv CSV             Override output CSV file path
  --html HTML           Override output HTML map file path
  --check-config        Validate the configuration and exit, without using the
                        network
  -q, --quiet           Suppress detailed logs
  --version             show program's version number and exit
```

`python -m flatscorer` takes the same arguments, for when the console script
isn't on your `PATH`.

### Checking a config without running it

`--check-config` runs the whole validation pass and stops there, touching
neither Nominatim nor Overpass:

```bash
$ flatscorer --config config.json --check-config
[+] 'config.json' is valid: 3 candidate(s), 2 destination(s). No network requests were made.
```

A scoring run takes minutes and spends other people's bandwidth, so finding a
typo that way is a bad trade — this turns the edit-and-check loop into something
instant and free. It exits non-zero with the same messages a real run would
print, which is what makes it usable in a pre-commit hook or CI. With no
`--config` it checks the built-in demo.

## Requirements

- Python 3.9+
- Dependencies: `osmnx`, `networkx`, `geopandas`, `pandas`, `folium`,
  `shapely`, `requests`, `platformdirs`, plus `scipy` and `scikit-learn` — osmnx
  treats those last two as optional, but nearest-node lookup needs them, so
  routing fails mid-run without them

Install everything with:

```bash
pip install flatscorer       # from PyPI
pip install -e .             # or from a checkout — dependencies and the package itself
```

`pip install -r requirements.txt` installs only the dependencies, not the
package, so it isn't enough to run the tool from a checkout on its own — CI
pairs it with `pip install --no-deps -e .`.

## Overpass API Resilience

The Overpass API (used for OSM data) can be flaky. FlatScorer automatically
retries failed requests and rotates through three public mirrors:

- `overpass-api.de`
- `overpass.kumi.systems`
- `overpass.private.coffee`

If all mirrors fail, check your internet connection or try again later — the
OSM infrastructure occasionally has outages.

**One response is not retried and does not fall through to the next mirror: a
`429 Too Many Requests`.** That is not an outage, it is the server asking this
client to stop, so the run stops with a message telling you how long to wait.
Retrying it, or putting the same query to a different mirror, is how a temporary
throttle becomes a ban — and since FlatScorer identifies itself by name in its
`User-Agent`, a ban would land on the project rather than on one anonymous IP.
Anything already downloaded is cached, so a later run resumes from there.

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
because CI installs from them. The code lives in `src/flatscorer/`, a src
layout, so nothing is importable without being installed and the tests exercise
the same package a user gets. `pytest` finds it via
`[tool.pytest.ini_options] pythonpath`.

| Module | Owns |
| --- | --- |
| `config` | `DEFAULT_CONFIG` and `validate_config` |
| `geocode`, `osm` | Nominatim and Overpass — the only network access |
| `spatial`, `routing` | Projection, the search-area guard, per-mode routing |
| `scoring` | Normalization, weighting, the score breakdown |
| `mapping` | The Folium map |
| `scorer` | `FlatScorer`, which drives all of the above in order |
| `cli`, `launcher` | The `flatscorer` and `flatscorer-gui` entry points |
| `paths` | Every runtime path, in one place |
| `gui/` | The Streamlit front end — `app.py` plus `views/`, one module per nav page |

Each leaf module owns the `DEFAULT_*` constants for its own concern and
`config` imports downward from them, so the layering stays acyclic. `flatscorer`
re-exports the public names lazily (PEP 562), which keeps `import flatscorer`
from dragging in osmnx, geopandas and folium.

Two rules apply to `gui/` specifically. Everything there uses **absolute
imports**, because `streamlit run` takes a path and Streamlit `exec`s `app.py`
with no package context — a relative import raises ImportError at runtime. And
the view modules live in `views/`, never `pages/`: a `pages/` directory beside
the entrypoint triggers Streamlit's multipage convention, which renders a second
navigation menu on top of the app's own.

Test modules mirror the source modules — `test_config`, `test_scoring`,
`test_spatial`, `test_osm`, `test_routing`, `test_geocode`, `test_mapping`,
`test_cli`, `test_run` — plus `test_gui` for the Streamlit front end via `streamlit.testing`
and `test_api` for the package-layout invariants (the lazy re-export table
resolving, `import flatscorer` staying free of the geo stack, the two `gui/`
rules above). Shared fixtures live in `tests/conftest.py`. Both `pytest` and
`ruff check` gate CI.

A `package` CI job builds the wheel and installs it into a clean venv with no
checkout on `sys.path`, so a module missing from `packages` or a broken console
script fails in CI rather than for whoever runs `pip install` first.

Releases go out from `.github/workflows/release.yml`, triggered by pushing a
`vX.Y.Z` tag. A PyPI version can never be re-uploaded or reused, so every check
runs *before* the upload: the tag has to agree with `version` in
`pyproject.toml`, and a wheel is rebuilt from the sdist and installed clean to
prove the sdist stands on its own (`[tool.setuptools.dynamic]` reads
`requirements.txt` at build time, so an sdist missing that file builds here and
is broken forever on PyPI). Running the workflow manually publishes to TestPyPI
instead, which is the rehearsal path that leaves the real version unburned.

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

MIT — see [LICENSE](https://github.com/Aduneer/FlatScorer/blob/main/LICENSE).
