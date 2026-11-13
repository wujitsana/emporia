"""One version for the whole project: the Python package, the plugin and the dashboard say the same number.

The agent card's client label and the dashboard header show the runtime package's number, so a package that said
0.3.0 beside a plugin released as 2.0.1 made agents look like they ran a plugin 0.3.0.
"""
import json
import re
import tomllib
from pathlib import Path

import emporia
from emporia import wire

ROOT = Path(__file__).resolve().parents[1]


def _plugin_yaml_version() -> str:
    m = re.search(r"^version:\s*\"?([^\s\"#]+)", (ROOT / "hermes_plugin/emporia/plugin.yaml").read_text(), re.M)
    assert m, "plugin.yaml has no top-level version"
    return m.group(1)


def test_every_place_names_the_same_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    manifest = json.loads((ROOT / "hermes_plugin/emporia/dashboard/manifest.json").read_text())["version"]
    assert pyproject == emporia.__version__ == _plugin_yaml_version() == manifest


def test_the_version_is_a_fit_client_version():
    assert wire.clean_client_version(emporia.__version__) == emporia.__version__
