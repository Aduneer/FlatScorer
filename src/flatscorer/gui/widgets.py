"""Choice lists shared by the editors."""

from __future__ import annotations

from flatscorer.routing import TRAVEL_MODES

ICON_CHOICES = [
    "home", "briefcase", "landmark", "train", "university", "hospital-o",
    "shopping-cart", "graduation-cap", "subway", "bus", "tree", "star",
]
COLOR_CHOICES = [
    "blue", "red", "green", "orange", "purple", "darkred", "darkblue",
    "darkgreen", "cadetblue", "darkpurple", "pink", "lightblue",
    "lightgreen", "gray", "black",
]

# Travel modes come from the engine rather than a literal here, so the dropdown
# can never offer a mode validate_config would reject.
MODE_CHOICES = list(TRAVEL_MODES)
MODE_EMOJI = {"walk": "🚶", "bike": "🚴"}
