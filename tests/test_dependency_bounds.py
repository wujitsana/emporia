"""Catalog rule 10: the runtime `hermes emporia setup` installs has a floor and an upper bound
on every dependency, and the versions uv.lock pins sit inside those ranges."""
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
LOCK = tomllib.loads((REPO / "uv.lock").read_text())


def _runtime_requirements() -> list[Requirement]:
    project = PYPROJECT["project"]
    extras = project.get("optional-dependencies", {})
    # What setup installs: the dependencies and the extras a runtime uses (not dev).
    specs = project["dependencies"] + [s for name, group in extras.items() if name != "dev" for s in group]
    return [Requirement(s) for s in specs]


def test_every_runtime_dependency_has_a_floor_and_an_upper_bound():
    unbounded = [str(r) for r in _runtime_requirements()
                 if not any(s.operator in (">=", ">", "==", "~=") for s in r.specifier)
                 or not any(s.operator in ("<", "<=", "==", "~=") for s in r.specifier)]
    assert not unbounded, f"bare floors (rule 10): {unbounded}"


def test_the_dev_extra_and_dev_group_are_bounded_too():
    """A reviewer reads the whole file (rule 10): no bare floor anywhere, dev tools included, and the
    lock records the same specifiers."""
    extras = PYPROJECT["project"].get("optional-dependencies", {})
    dev = [Requirement(s) for s in extras.get("dev", []) + PYPROJECT.get("dependency-groups", {}).get("dev", [])]
    assert dev
    unbounded = [str(r) for r in dev if not any(s.operator in ("<", "<=", "==", "~=") for s in r.specifier)]
    assert not unbounded, f"bare floors (rule 10): {unbounded}"
    emporia = next(p for p in LOCK["package"] if p["name"] == "emporia")
    recorded = [(d["name"], d.get("specifier", "")) for d in emporia["metadata"]["requires-dist"]
                if d.get("marker") == "extra == 'dev'"]
    recorded += [(d["name"], d.get("specifier", "")) for d in emporia["metadata"]["requires-dev"]["dev"]]
    want = sorted((r.name, str(r.specifier)) for r in dev)
    assert sorted((n, str(Requirement(f"{n}{s}").specifier)) for n, s in recorded) == want


def test_the_locked_versions_are_inside_the_ranges():
    locked = {p["name"]: Version(p["version"]) for p in LOCK["package"] if "version" in p}
    outside = [f"{r.name} {locked[r.name]} not in {r.specifier}" for r in _runtime_requirements()
               if r.name in locked and locked[r.name] not in r.specifier]
    assert not outside, outside


def test_the_lock_records_the_same_ranges_as_pyproject():
    """The lock's own copy of the project's requirements matches pyproject, so `uv lock --check`
    and a reviewer reading either file see the same bounds."""
    emporia = next(p for p in LOCK["package"] if p["name"] == "emporia")
    recorded = {(d["name"], d.get("marker", "")): d.get("specifier", "")
                for d in emporia["metadata"]["requires-dist"]}
    for spec in PYPROJECT["project"]["dependencies"] + PYPROJECT["project"]["optional-dependencies"]["x402"]:
        r = Requirement(spec)
        marker = "extra == 'x402'" if spec in PYPROJECT["project"]["optional-dependencies"]["x402"] else ""
        got = recorded.get((r.name, marker))
        assert got is not None and set(re.split(r",\s*", got)) == {str(s) for s in r.specifier}, (r.name, got)
