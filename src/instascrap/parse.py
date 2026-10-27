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
from typing import Literal
from collections import Counter
from datetime import datetime, timezone

from .models import Media, Post

# The one container confirmed against a real capture: a `/graphql/query`
# response wraps the profile's post edges here. Walking the payload (rather
# than indexing straight to this key) keeps parse.py tolerant of whatever
# wrapper object Instagram puts around it.
#
# Public on purpose: browser.py needs to recognise "the response a profile
# visit is waiting for has arrived" (see has_profile_grid below), and that is
# knowledge about Instagram's payload shape, which belongs here and nowhere
# else -- this is the module that will need editing the day the container name
# changes, and one definition means that edit is the only one required.
PROFILE_GRID_CONTAINERS = ("xdt_api__v1__feed__user_timeline_graphql_connection",)

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
            if key in PROFILE_GRID_CONTAINERS and isinstance(value, dict):
                found.extend(value.get("edges", []))
            else:
                found.extend(_walk_edges(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_edges(item))
    return found


def grid_owner(payload) -> str | None:
    """Whose profile grid `payload` carries, casefolded -- or None when it
    carries no grid, or carries one whose nodes name no author.

    Decided by a majority vote over the nodes' own authors rather than by any
    single node, because Instagram puts the *co-author* in a node's `user` on
    a collab post: a grid legitimately holds a minority of nodes authored by
    somebody else (seen on alanritchson's real grid, where a
    reacherprimevideo collab sits among his own posts). The profile owner is
    the author of the overwhelming majority, so the mode is the reliable
    reading, and testing ownership node by node would throw away real posts.

    A tie reads as None rather than picking a side. None means "cannot tell"
    everywhere it is used, and the callers below treat that as permission,
    not refusal -- an account with two posts, one of them a collab, must not
    lose its grid to a coin flip.
    """
    authors: Counter[str] = Counter()
    for edge in _walk_edges(payload):
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        user = node.get("user")
        if not isinstance(user, dict):
            continue
        username = user.get("username")
        if isinstance(username, str) and username:
            authors[username.casefold()] += 1
    if not authors:
        return None
    ranked = authors.most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def _has_grid_container(payload) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in PROFILE_GRID_CONTAINERS:
                return True
            if _has_grid_container(value):
                return True
        return False
    if isinstance(payload, list):
        return any(_has_grid_container(item) for item in payload)
    return False


def has_profile_grid(payload, account: str | None = None) -> bool:
    """True if `payload` carries a profile-grid container at any depth -- and,
    when `account` is given, one that belongs to that account.

    Without `account` this is a structural check only: it doesn't care whether
    the edges inside are well-formed, just whether the container is present.
    That's what tells "the response a profile visit is waiting for has arrived"
    from "something else arrived" (typically a home-feed payload left over from
    warm_up), without duplicating the container name Instagram uses.
    extract_posts_counted stays the parser of record for whether what's inside
    is actually usable.

    `account` closes the hole that check alone left open. A grid response can
    arrive during the *next* profile's visit -- 48 of the first 75 archived
    posts landed under the wrong account that way, and worse, the foreign grid
    satisfied the structural check, so the account that received it looked
    like it had got its own grid and its real one was never waited for. So the
    two failures were one: an account whose grid is late loses its posts *and*
    poisons its successor's feed. Ownership is what tells the two payloads
    apart, since nothing else about them differs.

    An unknowable owner (grid with no authors, or a tie -- see grid_owner)
    passes: a profile with no posts at all still receives a grid, and refusing
    that would trade a wrong-account bug for a silently-empty-feed one.

    Known, accepted boundary: checks only that the container *key* is present,
    not that its value is a usable dict -- a nulled-out container would read as
    "grid received" indistinguishably from a genuinely empty one. Not seen in a
    real capture and not worth tightening speculatively."""
    if not _has_grid_container(payload):
        return False
    if account is None:
        return True
    owner = grid_owner(payload)
    return owner is None or owner == account.casefold()


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
LOGIN_REDIRECT_PATH = "/accounts/login"
_BLOCK_PATHS = (LOGIN_REDIRECT_PATH, "/challenge")


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


def refusal_kind(status: int, payload: dict | None, url: str) -> Literal["blocked", "logged_out"] | None:
    """Sort a refusal into the two responses it calls for.

    "logged_out" (a bare redirect to the login or challenge pages) needs a human
    to sign in again and gains nothing from waiting; "blocked" (401/429 or a
    refusal marker in the payload) needs a backoff and must never open a login
    screen. When one response carries both, "blocked" wins: backing off is the
    safe mistake, opening a browser on an account that is refusing is not."""
    if detect_block(status, payload, url) is None:
        return None
    if status in (401, 429):
        return "blocked"
    if payload is not None:
        blob = json.dumps(payload)
        if any(marker in blob for marker in _BLOCK_MARKERS):
            return "blocked"
    # detect_block fired on neither the status nor a marker, so only one of
    # _BLOCK_PATHS is left: a redirect to the login or challenge page.
    return "logged_out"


def is_bare_login_redirect(status: int, payload: dict | None, url: str) -> bool:
    """True when the only block signal `detect_block` would find in this
    response is the plain redirect to Instagram's login page -- not a
    status-code refusal, not a challenge-path redirect, and not a JSON marker
    anywhere in the payload.

    This exists for `--login` mode, whose whole point is a *deliberate*
    navigation to the login page: a caller that expects to land there needs
    to tell the difference between "arrived where I meant to go" and "was
    redirected here mid-session", which `detect_block` cannot do -- it only
    sees a status, a payload and a URL, never the caller's intent, and it
    stays that way on purpose (see browser.Session.expect_login_redirect).

    Deliberately as strict as detect_block is trigger-happy: this only ever
    returns True for the single narrowest case, so every other signal --
    including one riding on the very same response as the login redirect --
    is still reported as a real block by the caller."""
    if status in (401, 429):
        return False
    if LOGIN_REDIRECT_PATH not in url:
        return False
    if "/challenge" in url:
        return False
    if payload is not None:
        blob = json.dumps(payload)
        if any(marker in blob for marker in _BLOCK_MARKERS):
            return False
    return True
