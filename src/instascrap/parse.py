"""Instagram JSON -> Post. Pure and I/O-free by design: this is the module that
breaks when Instagram reshapes its responses, and fixtures make that a five-minute
fix rather than a scraping session.

The node shape here was reverse-engineered from one real capture
(tests/fixtures/real_profile.json), not from Instagram's public docs -- those
describe an `edge_owner_to_timeline_media` / `shortcode` / `display_url` shape
that turned out to not exist in the wild at all. That invented shape (and its
container key) has been dropped entirely rather than kept as a dead fallback.
"""

import json
from datetime import datetime, timezone

from .models import Media, Post

# The one container confirmed against a real capture: a `/graphql/query`
# response wraps the profile's post edges here. Walking the payload (rather
# than indexing straight to this key) keeps parse.py tolerant of whatever
# wrapper object Instagram puts around it.
_EDGE_CONTAINERS = ("xdt_api__v1__feed__user_timeline_graphql_connection",)

# media_type on a node: 1 = single image, 2 = single video, 8 = carousel.
_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2
_MEDIA_TYPE_CAROUSEL = 8


def _caption(node: dict) -> str:
    # caption is a dict with a "text" field, or JSON null on posts with no
    # caption at all -- not an empty edges list like the invented shape assumed.
    caption = node.get("caption")
    if isinstance(caption, dict):
        return caption.get("text") or ""
    return ""


def _area(candidate: dict) -> int:
    return int(candidate.get("width", 0) or 0) * int(candidate.get("height", 0) or 0)


def _valid_list_of_dicts(value) -> list[dict]:
    """Shape guard for the three list-of-rendition-dicts boundaries this parser
    reads (`image_versions2.candidates`, `video_versions`, `carousel_media`).

    The old flat node shape had nothing this deeply nested; a non-dict entry
    here used to be unreachable and is now one `.get()` away from an
    AttributeError that would abort the whole harvest, not just this node. If
    any entry isn't a dict, the whole list is treated as unusable (not
    filtered down to the good entries) -- a reshape that puts garbage in this
    list is a sign the list's meaning changed, and guessing which entries are
    still trustworthy is worse than dropping the node cleanly.
    """
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    return []


def _best_image_candidate(node: dict) -> dict | None:
    """The rendition to archive for a still image (or a video's poster frame).

    Images are archived permanently and shown full-size in a reader, so there is
    no downside to the largest rendition on offer -- more pixels only helps, and
    it is a one-time download. `image_versions2.candidates` is not sorted by
    size (crops and resolutions are interleaved), so this scans all of them
    rather than trusting the first entry.
    """
    image_versions2 = node.get("image_versions2")
    raw_candidates = image_versions2.get("candidates") if isinstance(image_versions2, dict) else None
    candidates = _valid_list_of_dicts(raw_candidates)
    if candidates:
        return max(candidates, key=_area)
    # Some nodes carry a single `display_uri` instead of a candidates list.
    display_uri = node.get("display_uri")
    if isinstance(display_uri, str) and display_uri:
        return {
            "url": display_uri,
            "width": node.get("original_width", 0),
            "height": node.get("original_height", 0),
        }
    return None


def _best_video_candidate(node: dict, candidates: list[dict]) -> dict:
    """The rendition to archive for a video.

    Unlike images, "biggest" is not obviously "best" here: video_versions can
    carry multiple encodes that report inflated width/height without a matching
    quality gain, and archiving an oversized re-encode wastes disk for a post
    that is watched once. What we do have is `original_width`/`original_height`
    -- the source resolution -- so the rule is the rendition with the largest
    area that does not exceed that source resolution, falling back to the
    largest available if every candidate exceeds it (missing metadata, or an
    unusual capture). Ties resolve to the first-listed candidate.
    """
    orig_w, orig_h = node.get("original_width"), node.get("original_height")
    cap = (orig_w or 0) * (orig_h or 0)
    pool = [c for c in candidates if _area(c) <= cap] if cap else []
    return max(pool or candidates, key=_area)


def _leaf_media(node: dict) -> Media:
    """A node that is itself one playable/viewable unit: a plain post, or one
    entry of a carousel's `carousel_media`."""
    media_type = node.get("media_type")

    if media_type == _MEDIA_TYPE_VIDEO:
        video_candidates = _valid_list_of_dicts(node.get("video_versions"))
        if not video_candidates:
            raise KeyError("video node carries no usable video_versions")
        chosen = _best_video_candidate(node, video_candidates)
        poster = _best_image_candidate(node)
        return Media(
            kind="video",
            url=chosen["url"],
            width=int(chosen.get("width", 0)),
            height=int(chosen.get("height", 0)),
            # No duration field has been observed anywhere in a real node;
            # Media.duration stays optional for exactly this case.
            duration=None,
            path=None,
            poster_url=poster["url"] if poster else None,
        )

    if media_type == _MEDIA_TYPE_IMAGE:
        chosen = _best_image_candidate(node)
        if chosen is None:
            raise KeyError("image node carries no usable candidates")
        return Media(
            kind="image",
            url=chosen["url"],
            width=int(chosen.get("width", 0)),
            height=int(chosen.get("height", 0)),
            duration=None,
            path=None,
        )

    raise ValueError(f"unsupported leaf media_type {media_type!r}")


def _post_from_node(node: dict, account: str) -> Post | None:
    code = node.get("code")
    if not code or "taken_at" not in node:
        return None

    media_type = node.get("media_type")
    if media_type == _MEDIA_TYPE_CAROUSEL:
        children = _valid_list_of_dicts(node.get("carousel_media"))
        if not children:
            raise KeyError("carousel post carries no usable carousel_media")
        media = tuple(_leaf_media(child) for child in children)
        is_carousel = True
    else:
        media = (_leaf_media(node),)
        is_carousel = False

    return Post(
        shortcode=code,
        account=account,
        timestamp_utc=datetime.fromtimestamp(node["taken_at"], tz=timezone.utc),
        caption=_caption(node),
        permalink=f"https://www.instagram.com/p/{code}/",
        media=media,
        is_carousel=is_carousel,
        like_count=node.get("like_count"),
        comment_count=node.get("comment_count"),
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


def extract_posts_counted(payload: dict, account: str) -> tuple[list[Post], int]:
    """Posts plus the number of nodes that were skipped.

    The count is the whole point: a silent skip is how a reshaped Instagram
    response turns into feeds that quietly stop growing. The caller logs it, and
    a session that drops everything it sees is visible in the logs on the day it
    starts rather than three weeks later."""
    posts: list[Post] = []
    dropped = 0
    for edge in _walk_edges(payload):
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            dropped += 1
            continue
        try:
            post = _post_from_node(node, account)
        except (KeyError, TypeError, ValueError, AttributeError):
            # A malformed node must never abort a whole session's harvest.
            # The isinstance checks in _valid_list_of_dicts/_best_image_candidate
            # already turn the known-shaped garbage into a clean drop; this is
            # the backstop for whatever shape neither of us has seen yet.
            dropped += 1
            continue
        if post is None:
            dropped += 1
            continue
        posts.append(post)
    return posts, dropped


def extract_posts(payload: dict, account: str) -> list[Post]:
    return extract_posts_counted(payload, account)[0]


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
