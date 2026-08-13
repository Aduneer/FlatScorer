# Design notes

Why FlatScorer works the way it does, and the measurements behind the choices
that weren't obvious. The [README](../README.md) covers what the tool does and
how to run it; this file covers the decisions, including the ones that were
tested and abandoned.

The short version: a comparison tool is only useful if its ranking means
something, so most of the design effort went into two questions — *is this
number comparable to that one?* and *would a cheaper measurement give the same
answer?*

---

## 1. The score is a weighted average of **normalized** metrics

The first working version weighted raw values. It doesn't work, and the failure
is instructive: the raw metrics arrive in wildly different units — amenity
counts in single digits, green space in tens of thousands of m², rent in
hundreds of euros, commutes in minutes. Multiply those by weights and the term
with the largest magnitude dominates regardless of what the weights say. A
weight of 0.1 on green space still outvoted a weight of 3.0 on commute time,
because m² is a big number and minutes is a small one.

So `normalize_metrics()` maps every metric onto 0–1 **first**, and only then are
the weights applied. `weight_shares()` normalizes the weights to sum to 1, which
makes the result a weighted *average* rather than a weighted sum — genuinely
bounded to 0–10 instead of drifting upward as you add criteria.

`score_breakdown()` is the real implementation and `compute_score()` just sums
its contributions. That ordering is deliberate: it means *"why did this flat
win?"* is answerable by construction rather than reconstructed after the fact.
The breakdown is what the overview report renders, one bar per criterion, each
track as wide as the points that criterion could contribute and filled with what
this flat actually earned.

## 2. Anchors are absolute, not candidate-relative

The tempting alternative is min-max normalization across the candidate set:
score each metric relative to the best and worst flat in *this* run. It is
easier, it needs no configuration, and it is wrong for this problem.

Under min-max, the best flat in every run scores 10 — including a run where all
five candidates are bad. Scores stop being comparable between runs, which
defeats the purpose of a tool you use repeatedly over a search that lasts weeks.
Adding one terrible candidate silently rescales everyone else.

Instead every metric normalizes against a fixed anchor you configure once:
`rent_budget_eur`, `commute_cap_min`, `noise_cap_m`, and a saturation point per
amenity class. A 7.4 in March means the same thing as a 7.4 in May. A run where
everything scores badly is a run where everything *is* bad, which is
information.

The cost is that the anchors have to be set sensibly, and that the defaults
encode assumptions about density that are not universal — see §6, where that
turns out to matter more than expected.

**How well does it work?** Informally but concretely: on a real shortlist, the
ranking reproduces the author's own hand-ranking of the top 3 in roughly 9 cases
out of 10. That is a small sample and self-assessed, so it is evidence rather
than proof — but it is the check that matters, and it is the reason the absolute
anchors survived.

## 3. Does the street network earn its download?

Routing walk and bike commutes over the real OSM street network is the single
most expensive thing FlatScorer does: **~21 MB per travel mode and roughly 80%
of a run's wall clock.** The obvious cheap substitute is straight-line distance
multiplied by a detour factor. The question is whether that changes the answer.

**Method.** Synthetic candidate sets at hand-picked coordinates in three cities
chosen for different obstacle types, geocoding patched out entirely. One real
scoring run per city, then every comparison re-scored **offline** through the
production `scoring` functions from captured metrics. Total network cost: 4
Overpass requests spaced over ~18 minutes; everything downstream ran cache-only.

| | Budapest | Berlin | Lisbon |
| --- | --- | --- | --- |
| obstacle | Danube, bridges 500–1000 m apart | Spree + rail viaduct | steep topography |
| detour ratio (median) | 1.347 | 1.215 | 1.242 |
| range | 1.14 – 1.62 | 1.08 – 1.39 | 1.05 – 1.53 |
| coefficient of variation | 0.091 | 0.067 | 0.083 |
| Spearman ρ vs. routed | 0.986 | 0.986 | 0.988 |
| pairwise inversions | 6/153 (3.9%) | 6/153 (3.9%) | 5/153 (3.3%) |
| worst rank move | 2 | 2 | 2 |
| commute error | mean 1.4 min, max 5.4 | mean 1.2 min, max 4.3 | mean 0.9 min, max 2.6 |

**The headline finding is that the detour factor cannot reorder a ranking.**
Ordering by `straight_line_distance × factor` *is* ordering by straight-line
distance — a constant multiplier reorders nothing. The commute credit is linear
with a clamp, so the factor reaches the score only through the `commute_cap_min`
cutoff. Measured: inversions stayed at exactly 6/153 at factors 1.15, 1.28, 1.45
and 1.60. The factor's only real jobs are making the *displayed minutes* honest
and placing candidates correctly relative to the cap.

Two things that contradicted the predictions going in:

- **Lisbon was expected to be the worst case and came out the cleanest.** Hills
  make routes indirect, but they make them indirect *uniformly* across a
  neighbourhood — which is exactly the constant-factor case a ranking absorbs.
  Barriers that are sparse and directional, like a river with occasional
  bridges, distort more than terrain that is bad everywhere.
- **Street layout drives the detour factor far more than city size does.**
  Washington DC, added later, measures **1.11** — a grid city is a genuinely
  different regime from Budapest's 1.347. The default is 1.25, the median of the
  four. The three-city European sample was not as representative as it looked,
  which is why any future measurement here should include a non-European city.

**Outcome: shipped as `routing_mode: "straight_line"`, opt-in, with exact
routing still the default.** The approximation is good enough that it belongs in
the tool, and not so good that it should be imposed on someone who didn't ask.
It is ~4x faster even against a *warm* cache (6.8 s vs 25.7 s on the demo
config), because loading and routing a city graph costs real time on its own;
cold, the gap is seconds versus minutes.

Worth being explicit about what this trades away: shortest-path search needs the
graph, and the graph is the 21 MB. There is no "add real routing back later,
locally" — for a run in that mode the approximation is permanent.

## 4. Walking and cycling are genuinely different networks

The cheap option here was routing bikes over the pedestrian graph and adjusting
the speed. Measured over a 4×4 km box in central Berlin:

| | nodes | edges |
| --- | --- | --- |
| walk network | 12,366 | 30,690 |
| bike network | 5,468 | 11,247 |

8,061 nodes exist only in the walk graph and 1,163 only in the bike graph. The
*same* trip at the *same* pace routes **39.0 minutes walking vs 43.6 cycling** —
12% apart, in the direction that says bikes take a longer way round, because
they can't use the stairs and cut-throughs that pedestrians can.

That 12% is the justification for downloading a second network when a config
mixes modes. A run whose destinations are all one mode downloads one graph.

## 5. Not every transit stop is worth the same

Every bus stop, tram stop and metro station counted as exactly 1 in the first
version. That flatters a flat on a quiet bus route and undersells one on a metro
line, which is the wrong answer for the question the tool exists to answer.

Stops now contribute **bus-stop equivalents**: `railway=station` 3.0,
`railway=halt` 2.0, `railway=tram_stop` 1.5, `highway=bus_stop` 1.0.

Two decisions in that sentence are worth spelling out.

**A bus stop stays at exactly 1.0, deliberately.** The saturation anchor for
transit was calibrated against headcounts, and §6 is the whole story of what
happens when a metric's meaning shifts underneath an anchor that was tuned for
the old one. Denominating the weights in bus-stop equivalents means the anchor
keeps the meaning it was calibrated with, and the change moves scores only where
the transit genuinely is better — rather than inflating everyone's and
re-tuning the amenity terms by accident.

**The weights are not configurable, and that is not an oversight.** They encode
a claim about service level — a metro runs more often and further than a bus —
which is a fact about transit, not a preference. How much transit matters *to
you* is already `weights.transit`. A second knob on the same axis would only be
a way to express the same opinion twice and then wonder which one was winning.

### The bug this quietly created

The interesting part is what the change did to code that was already correct.

Transit stops were deduped as one combined layer, because a stop mapped both as
a node and as an area should count once. `dedupe_features(keep="points")`
resolves such a pair by dropping the *area*. While every stop was worth 1, that
was right no matter which copy survived.

The moment the classes carry different weights, it is a bug: a station polygon
sitting on top of a bus-stop node is the copy that gets discarded, so a 3.0
feature silently becomes a 1.0 one. No error, no warning — just a slightly wrong
score in exactly the dense interchange areas the feature was added to reward.

The fix is ordering: **dedupe each class on its own, then weight, then
combine.** The general shape is worth remembering, because it is not a bug in
either piece of code — it is a bug in their combination, introduced by a change
to neither. Code that is correct only because two things happen to be equal will
fail silently the moment they stop being equal, and nothing in the type system
or the existing tests knows that.

One case is deliberately *not* deduplicated: a single pole tagged
`highway=bus_stop` **and** `railway=tram_stop`, which is how interchanges are
commonly mapped, matches both layers and earns 2.5. That stop really does give
you both services.

## 6. A second POI source, evaluated and rejected

Every POI in FlatScorer comes from one Overpass query. [Overture
Maps](https://overturemaps.org/) Places looked like a strictly better source:
cloud-native GeoParquet on public object storage, bbox-queryable, rebuilt
monthly, permissively licensed (CDLA-Permissive-2.0), with no rate limit and no
clause restricting what kind of application may use it.

The pre-registered acceptance rule was: **winner and top 3 stable in every
city.** 57 candidates across Budapest, Berlin, Lisbon and Washington, Overture
counts compared against the OSM counts the same runs produced, both re-scored
through the production scoring functions.

**It failed.** Lisbon's winner moved; Budapest and Berlin lost top 3; worst rank
move was 7 places.

The failure mode was the opposite of the expected one. Overture has **more**
POIs than OSM, not fewer — 1.0x to 4.7x — because Places carries commercial data
that nobody ever tagged into OSM. And crucially the excess is **non-uniform
across both category and city**: bakeries run ~1.0–1.6x while gyms run 1.5–3.9x.
A uniform shift would have been harmless; a differential one reorders things.

The generalisable finding, and the reason this is written down rather than
forgotten: **the saturation anchors are calibrated against OSM densities.** Half
credit for supermarkets at 2.0 assumes what OSM reports. Feed the same anchors a
source with 3x the count and every candidate saturates, so the amenity terms
stop discriminating between candidates at all. *Any* change of POI source
silently re-tunes the scoring and requires recalibrating the anchors with it.
That is a much less obvious coupling than it looks, and it applies equally to a
future OSM whose coverage has improved.

**What this did not show is that Overture is worse.** The test measured
*agreement with OSM*, not accuracy, and there is no ground truth here.
Washington's 4.67x is partly OSM barely tagging shops in that city. The taxonomy
mapping used was deliberately generous, so a tighter one would narrow the gap
rather than widen it. The honest conclusion is that swapping the source is a
recalibration project, not a drop-in replacement — so it is parked rather than
refuted.

## 7. Everything that can fail, fails before the download

Both hard errors — `ConfigError` from `validate_config()` and `SearchAreaError`
from `check_search_area()` — are raised **before any network call**.

This is worth more than it sounds. A mistyped address that geocodes to the wrong
continent produces a bounding box spanning thousands of kilometres, and the
first symptom without the guard is a multi-minute Overpass query that may simply
hang. Catching it in a pure, offline function turns a mysterious stall into an
immediate message naming the outlier.

`validate_config()` reports *every* problem at once rather than failing on the
first, because the alternative is a fix-rerun-discover-the-next-one loop against
a tool whose runs take minutes. Both exception types subclass `ValueError`, so
the GUI can catch broadly while the CLI and tests rely on the base type.

A related rule: **prefer a hard error over an interactive prompt.** `run()` is
driven by Streamlit as well as by the CLI, and a Streamlit script has no channel
to answer a `input()`.

## 8. Being a good client of public infrastructure

FlatScorer runs against volunteer-funded services — Nominatim for geocoding,
Overpass for map data. Both have usage policies with real teeth, and both are
easy to violate accidentally. What the tool does:

- **Geocoding is throttled to 1 request/second**, and the throttle holds a lock
  *across* the sleep. That detail is load-bearing: Streamlit runs every session
  in its own thread, so two browser tabs scoring at once are two threads in that
  function. A naive read-then-sleep-then-write lets them measure their wait
  against the same stale timestamp and fire together. The policy limit is global
  to the application, so serializing is the intent, not a cost.
- **Both endpoints are configurable at runtime** (`nominatim_url`, and a mirror
  list for Overpass). Nominatim's policy requires that an application be able to
  switch away from the public instance *without shipping a software update* —
  an architectural requirement, not a courtesy.
- **Responses are cached** to a per-user cache directory, so re-running a
  config re-downloads nothing.
- **A 429 ends the run.** It is not retried, and it does not fall through to the
  next Overpass mirror. The mirror list exists for servers that are down or
  broken; a rate-limit is neither, and both instinctive responses to one —
  retry, or ask a different server the same question — are what turn a throttle
  into a ban. This one is worth spelling out because the library's default is
  the opposite: osmnx handles 429 itself by sleeping 55 seconds and re-sending
  the same query recursively, without a bound, so the status code never reaches
  the caller and no amount of care at the call site can help. The interception
  is a `requests` response hook installed in `settings.requests_kwargs`, which
  osmnx splats into its Nominatim and Overpass calls alike — low enough to fire
  before the library's own retry, and general enough that one hook covers every
  service the package talks to.
- **Attribution travels with every output.** The CSV opens with an ODbL credit
  comment, the map carries the tile layer's attribution, and the overview report
  has its own credit block. The CSV's notice is deliberately comma-free: anyone
  who forgets `comment="#"` gets a single column *named after the notice*, which
  explains itself, instead of a silent misparse.

**One bug here is worth documenting because of how quietly it failed.**
`ox.settings` is a plain Python module, so assigning `ox.settings.useragent =
"FlatScorer/..."` succeeds whether or not that attribute means anything —
and osmnx 1.x called it `useragent` while 2.x calls it `http_user_agent`. The
rename left a dead attribute nothing read. Every request the package made went
out under osmnx's default user-agent, which is precisely what Nominatim's policy
rules out, for the entire life of the 2.x pin. Nothing raised, nothing warned.

The fix is a helper that refuses to write a setting the library does not already
define, turning a silent miscompliance into an import-time failure CI catches.
The general lesson, which applies to any library configured by attribute
assignment: **setting a config attribute is not evidence that the config took
effect** — read it back, or assert the name exists.

## 9. How the code is arranged

The module layering is acyclic and is meant to stay that way:

```
layer 0   paths  scoring  routing  spatial  osm  geocode
layer 1   config  mapping
layer 2   scorer
layer 3   cli  launcher  gui/
```

Each leaf module owns the `DEFAULT_*` constants for its own concern, and
`config` imports *downward* from the leaves to assemble `DEFAULT_CONFIG`. No
leaf imports `config`. The tempting shortcuts — a single shared `constants.py`,
or `routing` reaching back into `config` for a default — both close a cycle
immediately.

Two consequences worth knowing:

- **`scoring` is pure functions over plain numbers.** No I/O, no geospatial
  types, no configuration lookups. That is what makes the scoring maths testable
  without a network, and it is why the full suite runs offline in seconds.
- **The GUI is a second entry point to the same engine, never a second
  implementation.** It builds a config dict and calls `FlatScorer(config).run()`
  exactly as the CLI does. Anything a user can do in the browser they can
  reproduce from an exported `config.json` on the command line.

`flatscorer/__init__.py` re-exports the public names **lazily** (PEP 562), so
`import flatscorer` doesn't drag in osmnx, geopandas and folium. A test fails the
moment someone adds an eager import there.

## 10. Deliberately not done

- **Scraping listing portals.** Their terms forbid automated access, the EU sui
  generis database right covers extraction from a listing database, and the
  sites sit behind bot protection with churning markup — so any scraper would be
  per-site and permanently breaking. Candidates carry an optional `url` you
  paste yourself.
- **Per-city detour calibration.** Ruled out by the measurement in §3: since the
  factor cannot reorder a ranking, calibrating it per city would buy more honest
  displayed minutes and nothing else.
- **Min-max normalization across the candidate set.** See §2.
- **Routing bikes on the pedestrian graph.** See §4.
