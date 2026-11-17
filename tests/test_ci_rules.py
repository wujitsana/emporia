"""SECURITY.md's CI rule, held true: our own workflow never uses pull_request_target and holds no secret."""
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_our_workflows_never_use_pull_request_target_and_use_no_secrets():
    files = sorted(WORKFLOWS.glob("*.y*ml"))
    assert files, "no workflow found"
    for f in files:
        text = f.read_text()
        assert "pull_request_target" not in text, f"{f.name} runs outside code with this repo's secrets"
        assert "secrets." not in text, f"{f.name} uses a secret"
