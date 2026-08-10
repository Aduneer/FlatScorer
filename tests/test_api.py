"""Invariants of the package layout itself.

These pin the three things the restructure is built on and that nothing else
would notice breaking: the lazy re-export table, the cheap top-level import, and
the absolute-imports-only rule inside `flatscorer.gui`.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

import pytest

import flatscorer

PACKAGE_DIR = os.path.dirname(os.path.abspath(flatscorer.__file__))
GUI_DIR = os.path.join(PACKAGE_DIR, "gui")


def _python_files(root: str) -> list[str]:
    return [
        os.path.join(dirpath, name)
        for dirpath, _dirs, names in os.walk(root)
        for name in names
        if name.endswith(".py")
    ]


def test_every_exported_name_resolves():
    """A name that moves module without updating `_EXPORTS` fails here, not at a caller."""
    unresolved = []
    for name in flatscorer._EXPORTS:
        try:
            getattr(flatscorer, name)
        except AttributeError as e:
            unresolved.append(f"{name}: {e}")
    assert not unresolved, unresolved


def test_unknown_names_still_raise_attribute_error():
    """`__getattr__` must not turn a typo into an ImportError or a hang."""
    missing = "no_such_name"
    with pytest.raises(AttributeError):
        getattr(flatscorer, missing)


def test_all_lists_exactly_the_public_exports():
    assert flatscorer.__all__ == sorted(n for n in flatscorer._EXPORTS if not n.startswith("_"))


def test_importing_the_package_does_not_pull_in_the_geo_stack():
    """`import flatscorer` must stay cheap.

    osmnx, geopandas and folium are seconds of cold start, and the desktop build
    wants a window on screen before it pays that. An eager re-export in
    `__init__` — the obvious way to write it — would silently undo this, and
    nothing else in the suite would notice.
    """
    probe = (
        "import sys; import flatscorer; "
        "heavy = [m for m in ('osmnx', 'geopandas', 'folium', 'pandas') if m in sys.modules]; "
        "print(','.join(heavy))"
    )
    env = dict(os.environ, PYTHONPATH=os.path.dirname(PACKAGE_DIR))
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "", f"import flatscorer dragged in {out.stdout.strip()}"


def test_the_gui_package_never_uses_relative_imports():
    """`gui/app.py` is `exec`'d by Streamlit with no package context.

    A relative import there raises ImportError at runtime, which no unit test of
    a sibling module would catch. The rule is applied to the whole `gui` package
    rather than just `app.py` so that moving code into it stays safe.
    """
    offenders = []
    for path in _python_files(GUI_DIR):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                offenders.append(f"{os.path.relpath(path, PACKAGE_DIR)}:{node.lineno}")
    assert not offenders, f"relative imports under gui/: {offenders}"


def test_no_pages_directory_sits_beside_the_streamlit_entrypoint():
    """`pages/` beside the entrypoint is Streamlit's multipage convention.

    Streamlit auto-discovers `pages/*.py` and renders its own sidebar nav for
    them — which showed up as a second, duplicate menu underneath this app's own
    radio. The view modules live in `views/` so that never fires. Nothing else
    would catch a rename back: the app still runs, it just grows a stray menu.
    """
    assert not os.path.isdir(os.path.join(GUI_DIR, "pages")), (
        "a pages/ directory next to app.py makes Streamlit render a duplicate nav"
    )
    assert os.path.isdir(os.path.join(GUI_DIR, "views"))


def test_the_app_script_streamlit_runs_is_where_the_launcher_looks():
    from flatscorer import launcher

    assert os.path.isfile(launcher.app_path())
    assert os.path.samefile(launcher.app_path(), os.path.join(GUI_DIR, "app.py"))


def test_the_banner_ships_inside_the_package():
    """Loaded at runtime, so it must travel with the code rather than the repo."""
    from flatscorer import paths

    assert os.path.isfile(paths.resource_path("gui", "assets", "banner.svg"))
