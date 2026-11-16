"""Projects we support or watch are not named anywhere in the repository until their maintainers are asked.

The names are kept rot13-encoded so this file does not name them either. Standards (ERC-8004 and the like) are
fine; a specific collection, adapter or team is not.
"""

import codecs
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NAMES = tuple(codecs.decode(w, "rot13") for w in ("tbgpuv", "fyhvpr"))
ADDRESSES = ('0kq3nn6p13oq3n53r1qq562oos0s819p496761sr9s', '0k000000009q62675362n58911r3s32srps46s5r18')

# "sluice" is the retired first name of Spillway (another plugin's name); only the legacy alias, its test and the
# dashboard's matching alias may keep it. Built bundles carry the dashboard's alias.
ALLOWED = {"tests/test_no_project_names.py", "tests/test_spillway_legacy.py", "src/emporia/modules/spillway.py",
           "dashboard/src/setPlay.ts", "CHANGELOG.md"}
BUILT = re.compile(r"(^|/)(dist|assets)/|plugin\.js$")


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    for rel in out.splitlines():
        p = REPO / rel
        if p.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".mp4",
                                ".lock", ".bundle"} or not p.is_file():
            continue
        try:
            yield rel, p.read_text(errors="ignore")
        except OSError:
            continue


def test_no_tracked_file_names_a_supported_project():
    pattern = re.compile("|".join(re.escape(n) for n in NAMES), re.I)
    addrs = tuple(codecs.decode(a, "rot13").lower() for a in ADDRESSES)
    hits = []
    for rel, text in _tracked_text_files():
        if rel in ALLOWED or BUILT.search(rel):
            continue
        low = text.lower()
        if pattern.search(text) or any(a in low for a in addrs):
            hits.append(rel)
    assert not hits, f"a supported project is named in: {hits}"
