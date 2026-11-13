"""The dashboard's explorer table (chainlink.ts) says what networks.py says: a drift guard, so a link on a card
points where the runtime's table says, and every chain with a registry has a label and an address explorer."""
import re
from pathlib import Path

from emporia import networks

TS = (Path(__file__).resolve().parent.parent / "dashboard" / "src" / "chainlink.ts").read_text()


def test_every_registry_chain_has_the_same_label_and_address_explorer_in_the_dashboard():
    assert set(networks.ERC8004_REGISTRIES) <= set(networks.NETWORKS)
    for caip2 in networks.ERC8004_REGISTRIES:
        n, chain_id = networks.NETWORKS[caip2], caip2.split(":")[1]
        assert n.address_explorer, caip2
        m = re.search(rf'"{chain_id}": \{{ label: "([^"]+)", explorer: "([^"]+)" \}}', TS)
        assert m, f"chainlink.ts has no entry for {caip2}"
        assert (m.group(1), m.group(2)) == (n.label, n.address_explorer), caip2


def test_the_dashboard_knows_no_chain_the_runtime_has_no_registry_for():
    ids = set(re.findall(r'^\s+"(\d+)": \{ label:', TS, re.M))
    assert ids == {c.split(":")[1] for c in networks.ERC8004_REGISTRIES}
