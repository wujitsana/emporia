"""A lab is named owner/repo for an agent; the repo URL and the slug reach the same lab; PRs and issues too."""
import pytest

from emporia import lab_names
from test_labs import Lab, as_

LABS = [
    {"slug": "emporia", "subject": {"kind": "github_repo", "ref": "wujitsana/emporia"},
     "repo_url": "https://github.com/wujitsana/emporia.git"},
    {"slug": "scratch", "subject": {"kind": "other", "ref": "a notebook"}, "repo_url": ""},
]


@pytest.mark.parametrize("name", ["wujitsana/emporia", "Wujitsana/Emporia", "emporia",
                                  "https://github.com/wujitsana/emporia", "https://github.com/wujitsana/emporia.git",
                                  "https://github.com/wujitsana/emporia/pull/12"])
def test_every_name_of_a_lab_resolves_to_its_slug(name):
    assert lab_names.resolve(name, LABS) == ("emporia", "")


def test_no_match_lists_the_labs_as_slug_and_repo():
    slug, why = lab_names.resolve("someone/else", LABS)
    assert slug == "" and "emporia (wujitsana/emporia)" in why and "scratch" in why and "owner/repo" in why


def test_a_lab_is_named_owner_repo_first_and_keeps_its_slug_otherwise():
    assert lab_names.name_of(LABS[0]) == "wujitsana/emporia" and lab_names.name_of(LABS[1]) == "scratch"
    assert list(lab_names.named(LABS[0]))[0] == "lab_name" and lab_names.named(LABS[0])["slug"] == "emporia"


def test_a_pr_or_issue_by_url_or_bare_name_becomes_a_ref():
    assert lab_names.gh_ref("https://github.com/o/r/pull/7") == ("gh_pr:o/r#7", "")
    assert lab_names.gh_ref("https://github.com/o/r/issues/8") == ("gh_issue:o/r#8", "")
    assert lab_names.gh_ref("o/r#7", "merged") == ("gh_pr:o/r#7", "")
    assert lab_names.gh_ref("o/r#8", "fixed") == ("gh_issue:o/r#8", "")
    ref, why = lab_names.gh_ref("o/r#9", "closed")
    assert ref == "" and "gh_pr:o/r#9" in why and "gh_issue:o/r#9" in why
    assert lab_names.gh_ref("gh_pr:o/r#7", "merged") == ("gh_pr:o/r#7", "")
    assert lab_names.gh_ref("https://github.com/o/r")[1]


def test_the_tools_take_owner_repo_the_url_and_the_slug(client, tmp_path, monkeypatch):
    from emporia import mcp_server as m
    lb = Lab(client, tmp_path, monkeypatch)
    assert lb.create().status_code == 200
    monkeypatch.setattr(m, "AGENT_ID", lb.author)
    monkeypatch.setattr(m, "_base", lambda: "")
    monkeypatch.setattr(m, "_labs_here", lambda: client.get("/labs").json()["labs"])
    monkeypatch.setattr(m, "_bearer_request", lambda method, url, **k: getattr(client, method)(
        url, params=k.get("params"), headers=as_(lb.author)))
    tool = getattr(m.lab_threads, "fn", m.lab_threads)
    for name in (lb.subject, f"https://github.com/{lb.subject}", lb.slug):
        out = tool(name)
        assert out["success"], (name, out)
        assert out["lab"]["lab_name"] == lb.subject and out["lab"]["slug"] == lb.slug
    miss = tool("nobody/nothing")
    assert miss["success"] is False and f"{lb.slug} ({lb.subject})" in miss["error"]
    monkeypatch.setattr(m.httpx, "get", lambda url, **k: client.get(url))
    listed = tool()
    assert [x["lab_name"] for x in listed["labs"] if x["slug"] == lb.slug] == [lb.subject]


def test_a_bare_pr_name_in_reply_to_takes_its_kind_from_the_labs_threads(client, tmp_path, monkeypatch):
    from emporia import mcp_server as m
    lb = Lab(client, tmp_path, monkeypatch)
    assert lb.create().status_code == 200
    monkeypatch.setattr(m, "_base", lambda: "")
    monkeypatch.setattr(m, "_labs_here", lambda: client.get("/labs").json()["labs"])
    monkeypatch.setattr(m, "_bearer_request", lambda method, url, **k: getattr(client, method)(
        url, params=k.get("params"), headers=as_(lb.author)))
    assert m._gh_reply(f"https://github.com/{lb.subject}/pull/4", lb.author) == f"gh_pr:{lb.subject}#4"
    assert m._gh_reply("message:abc", lb.author) == "message:abc"
    with pytest.raises(ValueError, match="name the kind"):
        m._gh_reply(f"{lb.subject}#4", lb.author)          # no thread yet: the kind is not known
    rep = lb.report(lb.maint, lb.pr(4), "merged", lb.line(lb.author, "pr", 4))
    assert rep.status_code == 200, rep.text
    assert m._gh_reply(f"{lb.subject}#4", lb.author) == f"gh_pr:{lb.subject}#4"
