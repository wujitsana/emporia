"""Live tests (P248): real relay processes, real HTTP. Opt in with `pytest -m live tests/live`
(or scripts/live_relays.py). Every test here gets a fleet of its own, so one scenario's keys,
rotations and stopped relays never reach another."""
import pytest

from tests.live.harness import Fleet


def pytest_collection_modifyitems(items):
    for item in items:
        if "/tests/live/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.live)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    setattr(item, f"rep_{call.when}", outcome.get_result())


@pytest.fixture
def fleet(tmp_path, request):
    """Relays A and B (linked both ways), C (a stranger), a rogue operator in front of B and a
    relay linked to that rogue. On a failure, each relay's and agent's log tail is printed."""
    f = Fleet(tmp_path)
    try:
        f.up()
    except BaseException:
        print(f.logs())
        f.down()
        raise
    yield f
    failed = getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed
    if failed:
        print("\n" + f.logs())
    f.down()
