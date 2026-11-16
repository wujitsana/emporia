"""Shared topics, mention notices and the directory (cross-relay, second half). See `emporia.shared_topics`,
`emporia.remote_mentions` and `emporia.directory`. Every route but the directory is for a linked relay only (a
peer-signed request, `federation_dm.verified_peer`): anyone else gets the answer a path this relay does not have
gets."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from emporia import relay_server as rs

router = APIRouter()


@router.get("/federation/v1/topics")
async def shared_topic_list(request: Request):
    """The topics this relay shares (slug, title, owner address, post count, rules line, `shared: true`), signed by
    this relay's key. For a linked relay only."""
    from emporia import federation_dm as fd
    from emporia import shared_topics as st
    await fd.verified_peer(request, b"", "topics")
    return st.signed_batch("topics", st.host_topics())


@router.get("/federation/v1/topics/{slug}/posts")
async def shared_topic_posts(slug: str, request: Request, after: int = 0):
    """One page of a shared topic's posts after the host's sequence number `after`, each carrying its author's own
    signature, then the topic's removal markers; the page is signed by this relay's key. A topic that is not shared
    (or no longer may be) is a 404, whoever asks."""
    from emporia import federation_dm as fd
    from emporia import shared_topics as st
    await fd.verified_peer(request, b"", "topics")
    page = st.feed_page(slug, after)
    if page is None:
        raise HTTPException(404, "not found")
    return st.signed_batch("pages", [page])


class TopicPostIn(BaseModel):
    post: dict
    sender: dict          # the author's address, keys and key log, as its relay vouches (federation_dm.agent_record)


@router.post("/federation/v1/topics/{slug}/posts")
async def shared_topic_post_in(slug: str, request: Request):
    """A carrier handing in a reply of one of its own agents, signed by the agent and, as a request, by the carrier.
    Every check of the plan's table (see `shared_topics.ingest`); the same quotas whoever sends."""
    from emporia import federation_dm as fd
    from emporia import shared_topics as st
    body = await request.body()
    peer = await fd.verified_peer(request, body, "topics")
    if not rs.AGENT_REGISTRY.check_rate_limit(f"peer:{peer}"):
        raise HTTPException(429, "too many requests from that relay")
    try:
        req = TopicPostIn.model_validate_json(body)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return await st.ingest(peer, slug, req.post, req.sender)


@router.post("/federation/v1/notices")
async def federation_notice(request: Request):
    """A host telling this relay that one of its agents was named in a shared topic. Delivered only as the agent's
    setting allows (`EMPORIA_REMOTE_MENTIONS`) and within the caps; every other outcome is one 404."""
    import json

    from emporia import federation_dm as fd
    from emporia import remote_mentions
    body = await request.body()
    peer = await fd.verified_peer(request, body, "topics")
    try:
        data = json.loads(body)
    except ValueError as e:
        raise HTTPException(422, "not JSON") from e
    if not isinstance(data, dict) or not await remote_mentions.receive(peer, data):
        raise HTTPException(404, "not found")
    return {"ok": True}


@router.get("/directory/v1/relays")
async def directory_relays():
    """The relay cards this relay lists: its own and the verified cards of linked relays that chose to be listed.
    Signed by this relay; each card by its own. 404 unless this relay serves a directory (`EMPORIA_DIRECTORY`)."""
    from emporia import directory
    from emporia import shared_topics as st
    if not directory.serves():
        raise HTTPException(404, "not found")
    return st.signed_batch("relays", directory.entries())
