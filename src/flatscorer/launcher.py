"""Console-script launcher for the FlatScorer Streamlit GUI.

`streamlit run` takes a *file path*, not an importable module name, so an
installed `flatscorer-gui` command has to work out where `gui/app.py` actually
landed. `paths.resource_path` owns that question — site-packages for a normal
install, the checkout for an editable one, the unpacked bundle when frozen.

Importing `app` to locate it is not an option: it builds the whole page at import
time (`st.set_page_config` and `main()` run at module level), so importing it
outside a Streamlit runtime does real work and prints warnings.
"""

from __future__ import annotations

import os
import sys

from . import paths

APP_FILENAME = "app.py"


def app_path() -> str:
    """Absolute path to the Streamlit app shipped alongside this package."""
    return paths.resource_path("gui", APP_FILENAME)


def main():
    """Run the GUI, forwarding any extra arguments to `streamlit run`."""
    path = app_path()
    if not os.path.exists(path):
        sys.exit(f"Error: {APP_FILENAME} is missing from {os.path.dirname(path)}. Try reinstalling FlatScorer.")

    try:
        from streamlit.web import cli as stcli
    except ImportError:
        sys.exit(
            "Error: the GUI needs Streamlit, which is an optional dependency.\n"
            "    Install it with:  pip install 'flatscorer[gui]'"
        )

    # Streamlit's CLI reads sys.argv directly, so hand off by rewriting it.
    sys.argv = ["streamlit", "run", path, *sys.argv[1:]]
    return stcli.main()


if __name__ == "__main__":
    main()
