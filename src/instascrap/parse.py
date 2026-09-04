"""Instagram JSON -> Post. Pure and I/O-free by design: this is the module that
breaks when Instagram reshapes its responses, and fixtures make that a five-minute
fix rather than a scraping session."""

import json
from datetime import datetime, timezone

from .models import Media, Post

# Both envelopes seen so far wrap the same node shape. Rather than branch on the
# endpoint, walk the payload and collect every known edge container.
_EDGE_CONTAINERS = (
    "edge_owner_to_timeline_media",
    "xdt_api__v1__feed__user_timeline_graphql_connection",
)


def _caption(node: dict) -> str:
    edges = node.get("edge_media_to_caption", {}).get("edges", [])
    return edges[0]["node"]["text"] if edges else ""


def _media_from_node(node: dict) -> Media:
    is_video = bool(node.get("is_video"))
    dims = node.get("dimensions", {})
    return Media(
        kind="video" if is_video else "image",
        url=node["video_url"] if is_video else node["display_url"],
        width=int(dims.get("width", 0)),
        height=int(dims.get("height", 0)),
        duration=float(node["video_duration"]) if is_video and "video_duration" in node else None,
        path=None,
    )


def _post_from_node(node: dict, account: str) -> Post | None:
    shortcode = node.get("shortcode")
    if not shortcode or "taken_at_timestamp" not in node:
        return None

    children = node.get("edge_sidecar_to_children", {}).get("edges")
    if children:
        media = tuple(_media_from_node(child["node"]) for child in children)
        is_carousel = True
    else:
        media = (_media_from_node(node),)
        is_carousel = False

    return Post(
        shortcode=shortcode,
        account=account,
        timestamp_utc=datetime.fromtimestamp(node["taken_at_timestamp"], tz=timezone.utc),
        caption=_caption(node),
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=media,
        is_carousel=is_carousel,
        like_count=node.get("edge_liked_by", {}).get("count"),
        comment_count=node.get("edge_media_to_comment", {}).get("count"),
    )


def _walk_edges(obj) -> list[dict]:
    """Collect edge lists from any known container, at any depth."""
    found: list[dict] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _EDGE_CONTAINERS and isinstance(value, dict):
                found.extend(value.get("edges", []))
            else:
                found.extend(_walk_edges(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_edges(item))
    return found


def extract_posts(payload: dict, account: str) -> list[Post]:
    posts = []
    for edge in _walk_edges(payload):
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        try:
            post = _post_from_node(node, account)
        except (KeyError, TypeError, ValueError):
            # A malformed node must never abort a whole session's harvest.
            continue
        if post is not None:
            posts.append(post)
    return posts


_BLOCK_MARKERS = ("feedback_required", "challenge_required")
_BLOCK_PATHS = ("/accounts/login", "/challenge")


def detect_block(status: int, payload: dict | None, url: str) -> str | None:
    """Return a human-readable reason if Instagram is pushing back, else None.

    Deliberately trigger-happy: a false positive costs one skipped session, a false
    negative costs the account."""
    if status in (401, 429):
        return f"HTTP {status} on {url}"

    for path in _BLOCK_PATHS:
        if path in url:
            return f"redirected to {path}"

    if payload is not None:
        # The marker can sit anywhere in the tree; serialising beats walking.
        blob = json.dumps(payload)
        for marker in _BLOCK_MARKERS:
            if marker in blob:
                return f"{marker} in response from {url}"

    return None
