#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["instaloader", "browser-cookie3"]
# ///
"""Scrape the last 7 days of posts from public Instagram profiles via instaloader.

Usage:
    uv run scrape.py user1 user2                         # anonymous, gets rate-limited fast
    uv run scrape.py --load-cookies safari user1 user2   # first run: import browser session
    uv run scrape.py --login myuser user1 user2          # later runs: reuse saved session
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

import browser_cookie3
import instaloader

BROWSERS = {
    "brave": browser_cookie3.brave,
    "chrome": browser_cookie3.chrome,
    "chromium": browser_cookie3.chromium,
    "edge": browser_cookie3.edge,
    "firefox": browser_cookie3.firefox,
    "librewolf": browser_cookie3.librewolf,
    "opera": browser_cookie3.opera,
    "safari": browser_cookie3.safari,
    "vivaldi": browser_cookie3.vivaldi,
}


def import_browser_session(loader: instaloader.Instaloader, browser: str) -> None:
    """Adopt the Instagram session from a logged-in browser.

    Sessions created by instaloader's own login() are systematically rejected by
    Instagram (401 "Please wait a few minutes") — only browser-originated cookies
    survive. Requires being logged into instagram.com in that browser.
    """
    cookies = {c.name: c.value for c in BROWSERS[browser]() if "instagram" in c.domain}
    if "sessionid" not in cookies:
        sys.exit(f"No Instagram sessionid found in {browser} — log into instagram.com there first.")

    loader.context.update_cookies(cookies)
    username = loader.test_login()
    if not username:
        sys.exit(f"Cookies from {browser} rejected by Instagram — re-login in the browser.")

    loader.context.username = username
    loader.save_session_to_file()
    print(f"Logged in as {username}; session saved — next runs can use --login {username}")


def scrape_profile(loader: instaloader.Instaloader, username: str, since: datetime) -> None:
    profile = instaloader.Profile.from_username(loader.context, username)

    for post in profile.get_posts():
        # get_posts() yields newest-first, so once we're past the window we can stop early
        # instead of walking the entire post history.
        if post.date_utc < since:
            break
        loader.download_post(post, target=username)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape last week of posts from public IG profiles")
    parser.add_argument("usernames", nargs="+", help="Instagram usernames to scrape")
    parser.add_argument("--login", metavar="USER", help="reuse a previously saved session file")
    parser.add_argument(
        "--load-cookies",
        metavar="BROWSER",
        choices=sorted(BROWSERS),
        help=f"import session from a logged-in browser ({', '.join(sorted(BROWSERS))})",
    )
    parser.add_argument("--days", type=int, default=7, help="how far back to go (default: 7)")
    args = parser.parse_args()

    loader = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
    )

    if args.load_cookies:
        import_browser_session(loader, args.load_cookies)
    elif args.login:
        loader.load_session_from_file(args.login)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    for username in args.usernames:
        print(f"--- Scraping {username} ---")
        try:
            scrape_profile(loader, username, since)
        except instaloader.exceptions.ProfileNotExistsException:
            print(f"Profile not found: {username}", file=sys.stderr)
        except instaloader.exceptions.ConnectionException as exc:
            print(f"Rate-limited or connection error on {username}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
