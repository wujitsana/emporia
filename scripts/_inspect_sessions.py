#!/usr/bin/env python3
"""Inspect session actions via httpx (no curl pipes)."""
import json
import sys
import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8088"
with httpx.Client(timeout=10) as c:
    r = c.get(f"{base}/sessions")
    r.raise_for_status()
    sessions = r.json().get("sessions", [])
    for s in sessions[:5]:
        sid = s["session_id"]
        ar = c.get(f"{base}/sessions/{sid}/actions")
        n = len(ar.json().get("actions", [])) if ar.status_code == 200 else -1
        print(sid[-12:], s["status"], "step", s.get("step_number"), "actions", n)