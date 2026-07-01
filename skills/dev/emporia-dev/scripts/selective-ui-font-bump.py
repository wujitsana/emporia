#!/usr/bin/env python3
"""Multiply font-size in dashboard.css by SCALE; skip .e-kpi* and --font-size: 16px.

Usage:
  DASHBOARD_CSS=/path/to/dashboard/src/dashboard.css python3 selective-ui-font-bump.py
  SCALE=1.05 python3 selective-ui-font-bump.py   # gentler bump
"""
import os
import re
from pathlib import Path

SCALE = float(os.environ.get("SCALE", "1.1"))
default = Path(__file__).resolve().parents[3] / "emporia" / "dashboard" / "src" / "dashboard.css"
path = Path(os.environ.get("DASHBOARD_CSS", default))
if not path.is_file():
    raise SystemExit(f"Missing CSS: {path}")

lines = path.read_text().splitlines()
out = []
n = 0

def scale_line(line: str) -> str:
    if "--font-size: 16px" in line:
        return line
    if re.search(r"\.e-kpi", line):
        return line

    def repl(m):
        val = float(m.group(1))
        unit = m.group(2)
        if unit == "rem":
            new = round(val * SCALE, 4)
            s = f"{new:.4f}".rstrip("0").rstrip(".")
            return f"font-size: {s}rem"
        return f"font-size: {max(1, round(val * SCALE))}px"

    return re.sub(r"font-size:\s*([0-9.]+)(rem|px)", repl, line)

for line in lines:
    if "font-size:" in line:
        new = scale_line(line)
        if new != line:
            n += 1
        line = new
    out.append(line)

path.write_text("\n".join(out) + "\n")
print(f"{path}: bumped {n} lines (x{SCALE}), skipped .e-kpi* and 16px root")