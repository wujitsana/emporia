"""docs/protocol.md names every signed message the code defines — a new one
cannot ship undocumented, and a removed one cannot linger in the spec."""
import inspect
import re
from pathlib import Path

from emporia import federation_dm, identity, wire

SPEC = (Path(__file__).resolve().parents[1] / "docs" / "protocol.md").read_text()


def test_every_agent_signed_payload_is_in_the_spec():
    builders = [n for n, f in inspect.getmembers(wire, inspect.isfunction)
                if n.startswith("signed_") and f.__module__ == "emporia.wire"]
    assert builders, "no payload builders found"
    missing = [n for n in builders if f"wire.{n}" not in SPEC]
    assert not missing, f"signed payloads not in docs/protocol.md: {missing}"


def test_every_signing_label_in_the_code_is_in_the_spec():
    sources = "".join(inspect.getsource(m) for m in (identity, federation_dm))
    labels = set(re.findall(r'b?"(emporia[:-][a-z-]+(?::[a-z-]+)*:v\d+)', sources))
    assert labels, "no labels found"
    missing = [label for label in labels if label not in SPEC]
    assert not missing, f"signing labels not in docs/protocol.md: {missing}"


def test_every_relay_signed_batch_kind_is_in_the_spec():
    import emporia
    root = Path(emporia.__file__).parent
    kinds = set()
    for path in root.rglob("*.py"):
        kinds |= set(re.findall(r'federation_signable_bytes\([^,]+,\s*"([a-z_-]+)"', path.read_text()))
    missing = [k for k in kinds if f"`{k}`" not in SPEC]
    assert not missing, f"relay-signed batch kinds not in docs/protocol.md: {missing}"
