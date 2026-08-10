"""The Streamlit front end.

`app.py` is the script `streamlit run` is pointed at; everything else here is an
ordinary importable module. Nothing in this package is imported by the engine —
the dependency runs one way, which is what keeps `streamlit` an optional extra.

Empty on purpose: importing the submodules here would pull `streamlit` in for
anyone who merely touches `flatscorer.gui`.
"""

from __future__ import annotations
