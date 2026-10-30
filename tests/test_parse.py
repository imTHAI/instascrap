import json
from datetime import timezone
from pathlib import Path

from instascrap.parse import (
    extract_posts,
    extract_posts_counted,
    grid_owner,
    has_profile_grid,
    profile_pic_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def raw_edges():
    payload = load("real_profile.json")
    return payload["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]["edges"]


def largest(candidates):
    """Independent re-implementation of "pick the biggest rendition", used to
    check the parser's choice against the raw fixture without importing the
    parser's own selection helper -- otherwise the test would just restate the
    implementation instead of verifying it."""
    return max(candidates, key=lambda c: c["width"] * c["height"])


# --- the real capture: two posts, a video (media_type 2) and a carousel (media_type 8) ---


def test_extracts_both_posts_from_the_real_capture():
    posts, dropped = extract_posts_counted(load("real_profile.json"), "alice")
    assert [p.shortcode for p in posts] == ["DWB9PQNDT3p", "CrzD4HbOMzu"]
    assert dropped == 0


def test_video_post_fields_match_the_raw_node():
    node = raw_edges()[0]["node"]
    post = extract_posts(load("real_profile.json"), "alice")[0]

    assert post.account == "alice"
    assert post.permalink == "https://www.instagram.com/p/DWB9PQNDT3p/"
    assert post.timestamp_utc.tzinfo == timezone.utc
    assert post.timestamp_utc.timestamp() == node["taken_at"]
    assert post.like_count == node["like_count"]
    assert post.comment_count == node["comment_count"]
    assert post.is_carousel is False
    assert post.caption == node["caption"]["text"]


def test_video_media_picks_a_rendition_matching_source_resolution_and_keeps_a_poster():
    node = raw_edges()[0]["node"]
    post = extract_posts(load("real_profile.json"), "alice")[0]

    assert len(post.media) == 1
    media = post.media[0]
    assert media.kind == "video"

    # All three video_versions entries in the fixture tie at the source resolution
    # (720x1280, matching original_width/original_height) -- the selection must
    # land on one of those, not on some other width/height.
    assert media.width == node["original_width"]
    assert media.height == node["original_height"]
    assert media.url in {v["url"] for v in node["video_versions"]}

    # The poster is the largest available frame from image_versions2 -- computed
    # independently here rather than trusting the parser's own math.
    expected_poster = largest(node["image_versions2"]["candidates"])
    assert media.poster_url == expected_poster["url"]


def test_carousel_expands_all_children_as_images():
    node = raw_edges()[1]["node"]
    post = extract_posts(load("real_profile.json"), "alice")[1]

    assert post.is_carousel is True
    assert post.permalink == "https://www.instagram.com/p/CrzD4HbOMzu/"
    assert len(post.media) == len(node["carousel_media"]) == 7
    assert all(m.kind == "image" for m in post.media)
    assert all(m.poster_url is None for m in post.media)

    for media, child in zip(post.media, node["carousel_media"]):
        expected = largest(child["image_versions2"]["candidates"])
        assert media.url == expected["url"]
        assert media.width == expected["width"]
        assert media.height == expected["height"]


# --- edge cases in the real schema, built from literal real-shaped fields ---
# (Not "synthetic fixtures" in the sense the design doc warns against -- these
# are minimal instances of the *confirmed* real schema, used to exercise paths
# the trimmed two-post capture doesn't happen to cover.)


def envelope(*nodes):
    return {
        "data": {
            "xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": n} for n in nodes]
            }
        }
    }


def image_node(**overrides):
    node = {
        "code": "IMG001",
        "taken_at": 1700000000,
        "media_type": 1,
        "caption": {"text": "hello"},
        "like_count": 10,
        "comment_count": 2,
        "original_width": 1080,
        "original_height": 1080,
        "image_versions2": {
            "candidates": [
                {"url": "https://cdn.test/small.jpg", "width": 320, "height": 320},
                {"url": "https://cdn.test/big.jpg", "width": 1080, "height": 1080},
                {"url": "https://cdn.test/mid.jpg", "width": 640, "height": 640},
            ]
        },
    }
    node.update(overrides)
    return node


def test_image_post_picks_the_largest_candidate_regardless_of_list_order():
    posts = extract_posts(envelope(image_node()), "alice")
    assert posts[0].media[0].url == "https://cdn.test/big.jpg"
    assert posts[0].media[0].width == 1080


def test_caption_is_empty_string_when_caption_is_null():
    posts = extract_posts(envelope(image_node(caption=None)), "alice")
    assert posts[0].caption == ""


def test_image_falls_back_to_display_uri_when_no_candidates():
    node = image_node(image_versions2={"candidates": []}, display_uri="https://cdn.test/only.jpg")
    posts = extract_posts(envelope(node), "alice")
    assert posts[0].media[0].url == "https://cdn.test/only.jpg"


def test_video_rendition_never_exceeds_source_resolution():
    node = image_node(
        media_type=2,
        original_width=720,
        original_height=1280,
        video_versions=[
            {"url": "https://cdn.test/upscaled.mp4", "width": 1440, "height": 2560},
            {"url": "https://cdn.test/native.mp4", "width": 720, "height": 1280},
            {"url": "https://cdn.test/low.mp4", "width": 480, "height": 854},
        ],
    )
    posts = extract_posts(envelope(node), "alice")
    media = posts[0].media[0]
    # The upscaled rendition is larger on paper but exceeds the source's own
    # resolution -- picking it would mean archiving an inflated re-encode for no
    # real quality gain. The native-resolution rendition is the right pick.
    assert media.url == "https://cdn.test/native.mp4"
    assert media.width == 720 and media.height == 1280


def test_carousel_can_mix_images_and_videos():
    carousel = image_node(
        media_type=8,
        carousel_media=[
            {
                "media_type": 1,
                "original_width": 1080,
                "original_height": 1080,
                "image_versions2": {"candidates": [{"url": "https://cdn.test/c1.jpg", "width": 1080, "height": 1080}]},
            },
            {
                "media_type": 2,
                "original_width": 720,
                "original_height": 1280,
                "image_versions2": {"candidates": [{"url": "https://cdn.test/c2-poster.jpg", "width": 720, "height": 1280}]},
                "video_versions": [{"url": "https://cdn.test/c2.mp4", "width": 720, "height": 1280}],
            },
        ],
    )
    posts = extract_posts(envelope(carousel), "alice")
    media = posts[0].media
    assert [m.kind for m in media] == ["image", "video"]
    assert media[1].url == "https://cdn.test/c2.mp4"
    assert media[1].poster_url == "https://cdn.test/c2-poster.jpg"


def test_carousel_with_no_children_is_dropped_not_crashed():
    posts, dropped = extract_posts_counted(envelope(image_node(media_type=8, carousel_media=[])), "alice")
    assert posts == []
    assert dropped == 1


def test_unsupported_media_type_is_dropped_not_crashed():
    posts, dropped = extract_posts_counted(envelope(image_node(media_type=99)), "alice")
    assert posts == []
    assert dropped == 1


def test_node_missing_code_or_taken_at_is_dropped():
    missing_code = image_node()
    del missing_code["code"]
    missing_taken_at = image_node()
    del missing_taken_at["taken_at"]
    posts, dropped = extract_posts_counted(envelope(missing_code, missing_taken_at), "alice")
    assert posts == []
    assert dropped == 2


def test_image_node_with_no_usable_url_is_dropped():
    node = image_node(image_versions2={"candidates": []})
    posts, dropped = extract_posts_counted(envelope(node), "alice")
    assert posts == []
    assert dropped == 1


# --- robustness: verifies the parser survives payloads that aren't Instagram at all ---


def test_malformed_candidate_entry_drops_that_node_but_not_others():
    # A non-dict entry in `candidates` used to reach `_area()`'s `.get()` call
    # unguarded and raise AttributeError, which was not in extract_posts_counted's
    # except tuple -- that took down the whole harvest, not just this node.
    bad = image_node(
        code="BAD",
        image_versions2={
            "candidates": [
                {"url": "https://cdn.test/good.jpg", "width": 100, "height": 100},
                "not-a-dict",
            ]
        },
    )
    good = image_node()  # code "IMG001", untouched
    posts, dropped = extract_posts_counted(envelope(bad, good), "alice")
    assert [p.shortcode for p in posts] == ["IMG001"]
    assert dropped == 1


def test_old_invented_container_yields_nothing():
    # edge_owner_to_timeline_media / shortcode / display_url was the pre-real-
    # capture invented shape (see the deleted web_profile_info.json /
    # timeline_graphql.json fixtures). Pinning this documents that dropping it
    # from PROFILE_GRID_CONTAINERS was deliberate, not an oversight.
    old_shaped = {
        "data": {"user": {"edge_owner_to_timeline_media": {"edges": [{"node": image_node()}]}}}
    }
    assert extract_posts(old_shaped, "alice") == []


def test_unrelated_payload_yields_nothing():
    assert extract_posts(load("unrelated.json"), "alice") == []


def test_garbage_payload_does_not_raise():
    assert extract_posts({"totally": ["unexpected"]}, "alice") == []


# --- has_profile_grid: what browser.py waits for during a profile visit ------

def test_has_profile_grid_true_for_the_real_capture():
    assert has_profile_grid(load("real_profile.json")) is True


def test_has_profile_grid_false_for_a_home_feed_payload():
    # The exact shape of the payload that masked the michellemonaghan defect:
    # a different, real Instagram container (the home feed's), not a garbage
    # or malformed one.
    home_feed = {"data": {"xdt_api__v1__feed__timeline__connection": {"edges": [
        {"node": image_node()}
    ]}}}
    assert has_profile_grid(home_feed) is False


def test_has_profile_grid_finds_the_container_at_any_depth():
    nested = {"wrapper": {"deeper": [{"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": []}}}]}}
    assert has_profile_grid(nested) is True


def test_has_profile_grid_false_for_unrelated_and_garbage_payloads():
    assert has_profile_grid(load("unrelated.json")) is False
    assert has_profile_grid({"totally": ["unexpected"]}) is False
    assert has_profile_grid([1, 2, "x"]) is False


# --- grid ownership: whose profile a grid payload actually belongs to --------

def grid_of(*authors):
    """A grid payload whose nodes are authored, in order, by `authors`."""
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": image_node(code=f"POST{index}", user={"username": author})}
        for index, author in enumerate(authors)
    ]}}}


def test_grid_owner_reads_the_real_capture_as_its_authors():
    assert grid_owner(load("real_profile.json")) == "michellemonaghan"


def test_grid_owner_is_the_majority_author_not_every_author():
    # A collab post carries its *co-author* in `user`, so a real grid holds a
    # minority of nodes authored by somebody else -- observed on alanritchson's
    # grid, where a reacherprimevideo collab sits among his own posts. Owning
    # the grid is a matter of majority, never of unanimity.
    assert grid_owner(grid_of("alan", "reacherprimevideo", "alan", "alan")) == "alan"


def test_grid_owner_is_none_when_it_cannot_be_told():
    assert grid_owner(grid_of()) is None                      # no nodes at all
    assert grid_owner(grid_of("alice", "bob")) is None        # a tie names nobody
    assert grid_owner({"totally": ["unexpected"]}) is None     # not a grid
    assert grid_owner(load("unrelated.json")) is None


def test_grid_owner_ignores_nodes_that_name_no_author():
    payload = grid_of("alice", "alice")
    payload["data"]["xdt_api__v1__feed__user_timeline_graphql_connection"]["edges"].append(
        {"node": image_node(code="ANON")}
    )
    assert grid_owner(payload) == "alice"


def test_has_profile_grid_rejects_another_accounts_grid():
    # The wrong-account defect: a late grid response arriving during the *next*
    # profile's visit satisfied the container check, so it read as "this
    # account's grid has arrived" twice over -- the account whose grid it was
    # never waited any longer for its own, and the account it arrived under
    # harvested it as its own posts.
    assert has_profile_grid(load("real_profile.json"), "clairegerhardstein") is False


def test_has_profile_grid_accepts_the_grid_of_the_account_asked_about():
    assert has_profile_grid(load("real_profile.json"), "michellemonaghan") is True
    assert has_profile_grid(load("real_profile.json"), "MichelleMonaghan") is True


def test_has_profile_grid_without_an_account_checks_only_the_container():
    assert has_profile_grid(grid_of("someone_else")) is True


def test_has_profile_grid_accepts_a_grid_whose_owner_cannot_be_told():
    # A profile with no posts at all still receives a grid, and its edges name
    # nobody. Refusing that would trade the wrong-account bug for a feed that
    # goes quietly empty -- so an unknowable owner passes.
    assert has_profile_grid(grid_of(), "alice") is True
    assert has_profile_grid(grid_of("alice", "bob"), "carol") is True


def test_extraction_keeps_a_post_authored_by_a_coauthor():
    # The counterpart to the majority rule above: ownership is decided for the
    # payload as a whole, never node by node. A collab post really is on the
    # grid it was found in, and dropping it would lose a real post from a real
    # feed.
    posts = extract_posts(grid_of("alan", "reacherprimevideo"), "alan")
    assert [p.shortcode for p in posts] == ["POST0", "POST1"]
    assert {p.account for p in posts} == {"alan"}


def _grid(*users):
    return {"data": {"xdt_api__v1__feed__user_timeline_graphql_connection": {"edges": [
        {"node": {"code": f"P{i}", "user": user}} for i, user in enumerate(users)]}}}


def test_profile_pic_prefers_the_hd_rendition():
    payload = load("real_profile.json")
    owner = next(e["node"]["user"] for e in raw_edges()
                 if e["node"]["user"]["username"] == "michellemonaghan")
    assert profile_pic_url(payload, "michellemonaghan") == owner["hd_profile_pic_url_info"]["url"]


def test_profile_pic_falls_back_to_the_standard_rendition():
    payload = _grid({"username": "alice", "profile_pic_url": "https://cdn.test/a.jpg"})
    assert profile_pic_url(payload, "alice") == "https://cdn.test/a.jpg"


def test_profile_pic_ignores_a_collab_co_author():
    # On a collab post the node's `user` is the co-author: their picture must
    # never become the followed account's icon.
    payload = _grid({"username": "bob", "profile_pic_url": "https://cdn.test/bob.jpg"},
                    {"username": "Alice", "profile_pic_url": "https://cdn.test/alice.jpg"})
    assert profile_pic_url(payload, "alice") == "https://cdn.test/alice.jpg"
    assert profile_pic_url(_grid({"username": "bob", "profile_pic_url": "x"}), "alice") is None


def test_profile_pic_tolerates_malformed_users():
    payload = _grid(None, {"username": "alice", "hd_profile_pic_url_info": "nope"},
                    {"username": "alice", "profile_pic_url": 42})
    assert profile_pic_url(payload, "alice") is None
