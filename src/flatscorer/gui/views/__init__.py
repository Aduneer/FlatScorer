"""One module per nav page. Each exposes `render()` and nothing else.

Named `views`, not `pages`, on purpose: a `pages/` directory beside the
entrypoint script is Streamlit's multipage convention, and it renders an
automatic sidebar nav on top of the radio this app already has.
"""

from __future__ import annotations

from flatscorer.gui.views import candidates, destinations, run, weights

__all__ = ["candidates", "destinations", "run", "weights"]
