"""The draft catalog entry (docs/catalog/emporia.yaml) agrees with the plugin it lists:
catalog rules 6 (capabilities), 13 (known issues disclosed) and 14 (requires_hermes, version)."""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ENTRY = yaml.safe_load((REPO / "docs" / "catalog" / "emporia.yaml").read_text())
PLUGIN = REPO / "hermes_plugin" / "emporia"
MANIFEST = yaml.safe_load((PLUGIN / "plugin.yaml").read_text())


def test_the_entry_names_the_plugin_dir_and_a_quoted_full_sha():
    assert ENTRY["subdir"] == "hermes_plugin/emporia" and ENTRY["tier"] == "community"
    assert isinstance(ENTRY["sha"], str) and len(ENTRY["sha"]) == 40      # quoted: zeros are not 0


def test_compatibility_metadata_matches_the_manifest():
    assert ENTRY["requires_hermes"] == MANIFEST["requires_hermes"]
    assert ENTRY["version"] == str(MANIFEST["version"])


def test_declared_capabilities_match_what_register_registers():
    caps = ENTRY["capabilities"]
    assert sorted(caps["provides_hooks"]) == sorted(MANIFEST["provides_hooks"])
    init = (PLUGIN / "__init__.py").read_text()
    for hook in caps["provides_hooks"]:
        assert f'ctx.register_hook("{hook}"' in init
    assert caps["provides_tools"] == [] and "register_tool" not in init
    assert caps["provides_middleware"] == [] and "register_middleware" not in init
    adapter = (PLUGIN / "adapter.py").read_text()
    assert f"required_env={caps['requires_env']!r}".replace("'", '"') in adapter


def test_vault_linked_keys_are_a_known_issue_in_the_entry_and_the_readme():
    assert any("cmd:" in issue and "password manager" in issue for issue in ENTRY["known_issues"])
    readme = (PLUGIN / "README.md").read_text()
    known = readme.split("## Known issues", 1)[1].split("\n## ", 1)[0]
    assert "cmd:" in known and "password manager" in known


def test_the_entry_and_pr_say_testnet_and_keep_to_the_repos_wording():
    pr = (REPO / "docs" / "catalog" / "PR.md").read_text()
    for text in (ENTRY["description"], pr):
        assert "testnet" in text.lower()
        assert "mainnet" not in text.lower() and "real money" not in text.lower()
