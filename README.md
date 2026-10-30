# instascrap

Follow public Instagram accounts as RSS feeds, without ever opening Instagram.

instascrap visits the profiles you list a few times a day, with a real Chrome
signed in to an Instagram account. It archives every post with its photos and
videos, then serves one RSS feed per account plus a combined feed. Media are
rehosted on your server, so your feed reader never loads anything from
Instagram.

It does **not** use Instagram's API, post or like anything, or read private
accounts you are not allowed to see.

> [!WARNING]
> Scraping breaks Instagram's terms of use. The account instascrap signs in
> with can be restricted or banned. **Use a dedicated account, never your
> personal one.** The defaults are deliberately slow; making them more
> aggressive raises the risk.

## Disclaimer

> [!CAUTION]
> **instascrap is provided strictly for educational and research purposes.**
> It is not affiliated with, endorsed by, or connected to Instagram or Meta
> Platforms, Inc. "Instagram" and "Meta" are trademarks of their respective
> owners.
>
> - Automated access to Instagram is prohibited by
>   [Instagram's Terms of Use](https://help.instagram.com/581066165581870) and
>   Meta's policies. By using this software, you accept that **you alone are
>   responsible** for how you use it, for complying with those terms, and for
>   complying with the laws that apply to you (copyright, data protection such
>   as the GDPR, computer misuse laws, and so on).
> - The posts, photos and videos instascrap archives belong to their authors.
>   Keep them for your own private use. Do not republish, redistribute or sell
>   them, and do not use this tool to track, profile or harass anyone.
> - Using instascrap can get the Instagram account it signs in with, and your
>   IP address, restricted or permanently banned. That risk is yours.
> - The software is provided "as is", without warranty of any kind (see
>   [LICENSE](LICENSE)). The authors and contributors accept **no liability**
>   for any damage, account loss, legal claim or other consequence arising from
>   its use or misuse.
>
> If you do not accept these conditions, do not use this software.

## Contents

- [How it works](#how-it-works)
- [Install on Unraid](#install-on-unraid)
- [First login](#first-login)
- [Settings](#settings)
- [How fresh are the feeds?](#how-fresh-are-the-feeds)
- [Reverse proxy](#reverse-proxy)
- [Alerts](#alerts)
- [Troubleshooting](#troubleshooting)
- [Advanced: serve the files with your own web server](#advanced-serve-the-files-with-your-own-web-server)
- [Manual commands](#manual-commands)
- [License](#license)

## How it works

One container, running all the time:

- **Runs** happen at the times in `SCHEDULE`, each delayed by a random amount
  so they never hit Instagram at the same second every day. A run visits a few
  profiles, with random pauses in between, and saves what is new.
- **Feeds** are served on port `8080`. You put your reverse proxy in front for
  HTTPS and a domain name.
- **Login**: when there is no Instagram session yet, or when Instagram logs it
  out, the container stops running, opens a login screen on port `6080` and
  tells you. You sign in by hand, 2FA included, then runs resume. instascrap
  never sees or stores your Instagram password.
- **Throttling**: if Instagram pushes back (rate limit, "feedback required"),
  instascrap pauses for 24 hours, then 72 hours if it happens again.

## Install on Unraid

1. Add the template, either way:
   - **Docker** tab → **Template repositories** (at the bottom of Docker
     settings): add `https://github.com/imTHAI/instascrap` and save. Then
     **Add Container** and pick `instascrap`.
   - Or copy [`unraid/instascrap.xml`](unraid/instascrap.xml) to
     `/boot/config/plugins/dockerMan/templates-user/my-instascrap.xml` on the
     flash drive, then **Add Container** → `instascrap`.
2. Fill in the three required fields: **Instagram accounts**, **Public URL**,
   **Login screen password**.
3. Apply. The first start prints the address of the feeds index page in the
   container log:

   ```
   Feeds index: https://feeds.example.com/3f9a1c0b7d2e4a68/index.html
   ```

   That page lists every feed URL to paste into your reader.

Outside Unraid, the same thing with `docker run`:

```bash
docker run -d --name instascrap --shm-size=1g --stop-timeout=30 \
  -v /path/to/instascrap:/data -p 8080:8080 -p 6080:6080 \
  -e ACCOUNTS=nasagoddard,map_of_europe \
  -e PUBLIC_URL=https://feeds.example.com \
  -e VNC_PASSWORD=change-me \
  -e TZ=Europe/Paris \
  ghcr.io/imthai/instascrap:latest
```

`--shm-size=1g` is required: Chrome crashes with Docker's default 64 MB of
shared memory. `--stop-timeout=30` gives Chrome, the login screen and the feed
server time to shut down cleanly instead of being killed after 10 seconds. The
Unraid template already sets both.

## First login

1. Open `http://<your-server-ip>:6080/vnc.html` in a browser **on your local
   network**, click **Connect** and enter `VNC_PASSWORD`.
2. You see Chrome on Instagram's login page. Sign in with the dedicated
   account and complete 2FA or any "Was this you?" check.
3. Within a few seconds the container notices the session, closes the login
   screen (port 6080 stops answering) and waits for the next scheduled run.

The same screen reopens by itself whenever Instagram logs the session out.
You get an alert (see [Alerts](#alerts)), repeated once a day until you sign in.

If you mapped the login port to another host port, use that port instead of
`6080`: the alert can only show the container's port.

## Settings

All settings are environment variables, filled in from the Unraid template.

| Variable | Default | What it does |
|:---|:---|:---|
| `ACCOUNTS` | *(required)* | Usernames to follow, comma-separated. `@name`, spaces and duplicates are cleaned up. |
| `PUBLIC_URL` | *(required)* | Base URL your feed reader uses to reach port 8080, e.g. `https://feeds.example.com` or `http://192.168.1.10:8080`. It is written into the feeds. |
| `VNC_PASSWORD` | *(required)* | Password of the login screen. VNC only uses the **first 8 characters**. |
| `SCHEDULE` | `08:00,14:00,21:00` | Run times, `HH:MM`, comma-separated, in the container's time zone. |
| `ACCOUNTS_PER_SESSION` | `7` | Profiles visited per run. |
| `FEED_ITEMS` | `50` | Maximum posts per feed (and in the combined feed). The archive keeps everything. |
| `MIN_GAP_SECONDS` | `25` | Shortest random pause between two profiles. |
| `MAX_GAP_SECONDS` | `90` | Longest random pause between two profiles. |
| `JITTER_MINUTES` | `30` | A run starts at a random time up to this many minutes after its slot. |
| `FEED_SECRET` | *(generated)* | Secret path segment in every URL (16 lowercase hex characters). Empty: generated on first start and kept in `/data/feed_secret`. |
| `DISCORD_WEBHOOK` | *(empty)* | Discord webhook URL for alerts. Empty: alerts only go to the container log. |
| `PUID` / `PGID` | `99` / `100` | Owner of the files in `/data` (Unraid's `nobody:users`). |
| `TZ` | `UTC` | Time zone for `SCHEDULE`. Unraid sets it for you from the server's settings. |

**About the feed secret.** Feeds and media are public files: anyone with a URL
can read it. The secret in the path is what keeps people who only know your
domain from finding them, a bit like a "share with link" URL. Don't post feed
URLs publicly. To keep existing URLs when you reinstall, copy the value of
`/data/feed_secret` into `FEED_SECRET`.
Changing `FEED_SECRET` revokes every old URL: at the next start the feeds
move to the new secret and the old path stops being served, which is how to
cut off a feed URL that leaked.

**Changing `PUBLIC_URL`** (for example when you move from the LAN address to
your reverse proxy): feeds are rebuilt at the next container start, but your
reader is subscribed to the old URLs, so re-subscribe.

## How fresh are the feeds?

Visits per day = number of `SCHEDULE` times × `ACCOUNTS_PER_SESSION`. Each run
visits the accounts that were visited longest ago, so every account comes
round every *accounts ÷ visits per day* days.

With the defaults, 3 × 7 = **21 visits a day**:

| Accounts followed | Each account visited every |
|:---|:---|
| 10 | ~11 hours |
| 21 | 1 day |
| 42 | 2 days |

A visit picks up the latest ~12 posts shown on the profile, so a post is only
missed if the account publishes more than that between two visits. Roughly:
up to ~20 accounts for a daily visit, ~40 for accounts that post rarely.
Following more accounts only makes visits less frequent; it is raising
`ACCOUNTS_PER_SESSION` or adding run times that makes the sessions look less
human.

## Reverse proxy

Point your reverse proxy at port `8080` over plain HTTP; it handles HTTPS.

> [!CAUTION]
> **Never expose port `6080` through your reverse proxy or to the internet.**
> Whoever reaches it, and guesses an 8-character password, drives a browser
> signed in to your Instagram account. Keep it on your LAN.

Caddy:

```
feeds.example.com {
	reverse_proxy 192.168.1.10:8080
}
```

Nginx Proxy Manager / SWAG: a proxy host for `feeds.example.com` forwarding to
`http://192.168.1.10:8080`, nothing else needed.

Until you have a reverse proxy, `PUBLIC_URL=http://<your-server-ip>:8080`
works on your local network.

## Alerts

With `DISCORD_WEBHOOK` set, the container posts to Discord. Every alert is
also written to the container log.

| Alert | Meaning |
|:---|:---|
| **Instagram login needed** | No session yet, or Instagram logged it out. Sign in on port 6080. Repeated daily. |
| **Logged in** | The session is back; runs resume at the next scheduled time. |
| **Instagram is throttling** | Instagram pushed back. Runs pause until the time shown. |
| **Capture broken** | No visited profile returned its posts: Instagram probably changed something. |
| **Missing grids** | Some profiles returned nothing this run. Usually a slow page; worry if it repeats. |
| **Partial failures** | Some posts or media could not be saved this run. |
| **No new posts** | Nothing new for over 7 days across all accounts. The parser may be broken. |
| **Run crashed** / **Login screen failed** | Unexpected error; details in the container log. |

## Troubleshooting

- **Container stops right away**: read the log. A configuration error names
  the variable at fault, e.g. `configuration error: SCHEDULE: '8h' is not a
  HH:MM time`.
- **Login screen does not load**: check port 6080 is mapped, and that the log
  says *Instagram login needed*. The screen is only open while a login is
  needed.
- **Throttled often**: lower `ACCOUNTS_PER_SESSION`, remove a run time, or
  raise the pauses. Don't log in again during a pause: opening a browser while
  Instagram is refusing makes things worse, which is why the login screen
  waits for the pause to end.
- **Feeds return 404**: the URL must contain the feed secret. Open the index
  page printed in the log.
- **Data directory owned by root after an older setup**: the container hands
  `/data` to `PUID:PGID` at start; the first start can take a while on a large
  archive.

## Advanced: serve the files with your own web server

Reverse-proxying to port 8080 is the recommended setup. If you would rather
have an existing web server read the files directly, mount `<data>/public`
read-only into it and serve it as the site root. You **must** reproduce what
the built-in server does, or you may leak the feed secret:

- no directory listing;
- `X-Robots-Tag: noindex, nofollow` on every response;
- nothing served outside `public/` (in particular not `feed_secret`, `state.json`
  or `profile/`, which holds the Instagram session);
- optionally, short feed URLs: `/<secret>/<account>` serving `<account>.xml`.

Caddy example:

```
feeds.example.com {
	root * /srv/instascrap/public
	try_files {path} {path}.xml
	file_server
	header X-Robots-Tag "noindex, nofollow"
}
```

## Manual commands

Run inside the container:

```bash
# Rebuild every feed from the archive
docker exec --user 99:100 instascrap python3 -m instascrap --republish

# Run once now, outside the schedule
docker exec --user 99:100 instascrap python3 -m instascrap --run

# Visit one profile and keep the raw responses in /data/raw (debugging)
docker exec --user 99:100 instascrap python3 -m instascrap --capture nasagoddard
```

`--user 99:100` (your `PUID:PGID`) is required: as root, files written to
`/data` would become unwritable for the container, so the commands refuse to
start. `--run` and `--capture` also refuse during an Instagram pause, while a
login is pending, or while the container is using the browser (a scheduled run
or the login screen); try again once it is done.

## License

[MIT](LICENSE). The license covers the code only; it grants no rights over
Instagram content, and it does not change anything in the
[Disclaimer](#disclaimer) above.
