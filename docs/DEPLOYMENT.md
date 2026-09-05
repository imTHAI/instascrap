# instascrap — mise en service

> Procédure manuelle à dérouler dans l'ordre. Tout ce qui pouvait être automatisé et
> vérifié l'a été ; ce qui reste demande soit tes identifiants, soit une décision qui
> t'appartient (édition du Caddyfile en production, dashboard Cloudflare), soit le
> compte Instagram dédié une fois réchauffé.

> [!warning]
> Ne lance rien de tout ceci avant que le compte dédié n'ait été utilisé à la main
> pendant environ une semaine. Une connexion prématurée est précisément ce qui fait
> flaguer un compte, et c'est ainsi que le compte personnel a été perdu.

## Handover runbook — ordered manual steps

These are the brief's Steps 3, 5, 6, 7, 8 (partially), 9, 10, 11 — none of which I
performed, per scope. Do them in this order; each depends on the one before it.

1. **Generate the prefix and deploy config.toml** (brief Step 3 — do not let an
   assistant invent account names here).
   ```
   python3 -c "import secrets; print(secrets.token_hex(8))"
   ssh tower "mkdir -p /mnt/user/appdata/instascrap"
   ```
   Copy `config.example.toml` to
   `/mnt/user/appdata/instascrap/config.toml` on tower, then edit it there:
   set `public_prefix` to the generated hex string, `accounts` to the real list,
   `data_dir = "/data"`, `publish_dir = "/publish"`. Success looks like: the file
   exists, `python3 -c "import tomllib,pathlib; tomllib.loads(pathlib.Path('config.toml').read_text())"`
   parses it without error on tower.

2. **Initial Instagram login** (brief Step 5 — needs the warmed-up dedicated account;
   do not run this until that account is ready).
   ```
   ssh tower "bash /mnt/user/appdata/instascrap-src/deploy/login.sh"
   ```
   Open `http://tower:6080/vnc.html`, log into the dedicated account, complete
   2FA, return to the terminal, press Enter. Success looks like:
   `/mnt/user/appdata/instascrap/profile/` contains a populated Chrome
   profile directory (non-empty, has `Default/` etc.).

3. **Capture a real payload and confront the parser** (brief Step 6 — the project's
   one real leap-of-faith step; fixtures so far are synthetic).
   ```
   ssh tower "docker run --rm \
     -v /mnt/user/appdata/instascrap:/data \
     -v /mnt/user/appdata/instascrap/config.toml:/app/config.toml:ro \
     --shm-size=1g instascrap:latest --capture UN_COMPTE_CIBLE"
   ```
   Pull the resulting `.json.gz` from `/data/raw/UN_COMPTE_CIBLE/` on tower,
   decompress it locally to `tests/fixtures/real_profile.json`, add
   `tests/test_parse_real.py` per the brief's Step 6 body, and adjust `parse.py` if
   the real shape differs from the synthetic fixtures. Run
   `uv run --with pytest pytest tests/ -v` — success looks like the whole suite
   passing, real fixture included.

4. **First full session** (brief Step 7).
   ```
   ssh tower "bash /mnt/user/appdata/instascrap-src/deploy/run.sh"
   ```
   Success looks like a log line `visited=[...] new_posts=N blocked=None` and XML
   files appearing under `/mnt/user/appdata/caddy/site/instacrap/<préfixe>/`.

5. **Insert the Caddy block and reload** (brief Step 8 — yours to approve; I did not
   touch the live file).
   Open `/mnt/user/appdata/caddy/config/Caddyfile` on tower and paste in
   the contents of `deploy/Caddyfile.snippet` from this repo (no placeholder to
   substitute — the snippet doesn't reference the prefix at all, see above). Then:
   ```
   ssh tower "docker exec Caddy caddy reload --config /etc/caddy/Caddyfile"
   ```
   Success looks like the reload command returning with no syntax error printed.

6. **Cloudflare dashboard steps** (brief Step 9 — manual, yours).
   Add the public hostname `feeds.example.com` pointing at Caddy through the tunnel,
   and regenerate the tunnel token (it was shown in clear during design). Success
   looks like the hostname resolving and the tunnel showing the new token active in
   the dashboard.

7. **End-to-end check** (brief Step 10).
   ```
   curl -sI https://feeds.example.com/<préfixe>/all.xml | head -3
   ```
   Success looks like `200 OK` with an XML content-type. Then add
   `https://feeds.example.com/<préfixe>/<compte>` to News Explorer — images should
   render and a video should play in place.

8. **Schedule in Unraid** (brief Step 11).
   User Scripts → new script pointing at `deploy/run.sh` (once it's copied to
   Unraid's script location, or referenced from `/mnt/user/appdata/instascrap-src/deploy/run.sh`),
   cron `0 8,14,21 * * *`. Success looks like the script listed as scheduled and a
   manual "Run script" trial completing with the same log line as step 4 above.

9. **Commit the real-capture test and fixture** (brief Step 12 — the part of the
   brief's single bundled commit that is still outstanding: `deploy/run.sh` and
   `deploy/Caddyfile.snippet` were already committed separately in commit `4e74eab`,
   so only the artefacts from step 3 above remain to commit).
   ```
   git add tests/test_parse_real.py tests/fixtures/real_profile.json
   git commit -m "🚀 chore: real-capture parser fixture"
   ```
   Success looks like `git log -1` showing this commit on `feat/instascrap` with
   exactly those two files, and `git status` clean afterward (aside from anything
   else you're mid-way through).

## Files changed

- `deploy/run.sh` (new, executable; amended post-review, see Fix report)
- `deploy/Caddyfile.snippet` (new)
- `.superpowers/sdd/2026-09-04-instascrap/task-12-report.md` (this report; amended post-review)
- Commit `4e74eab` on `feat/instascrap` (initial artefacts)
- Follow-up commit fixing the two review findings (see git log)

## Self-review

- **Completeness:** both artefacts written; every out-of-scope brief step (3, 5, 6, 7,
  8's Caddyfile edit/reload, 9, 10, 11) is recorded above as a handover item, in
  order, with exact commands and success criteria.
- **Quality:** `run.sh` fails safely (config-missing guard, verified above); mount
  paths/flags verified against the actual image's `Entrypoint`/`Cmd`/`Workdir` and
  against `config.py`/`main.py`'s own defaults, not just copied blind from the brief.
  The Caddy snippet was checked against the live Caddyfile's conventions (root/
  file_server/security_headers pattern, tab indentation, container mount mapping)
  and syntax-verified via `caddy adapt` inside the running container without
  touching or reloading the live file.
- **Discipline:** did not create `config.toml` on tower, did not touch Instagram,
  did not edit or reload the live Caddyfile, did not touch the Cloudflare dashboard.
  The only writes to tower were the `rsync` of source (as instructed) and two
  short-lived scratch files inside `/tmp` and the `Caddy` container, both deleted
  immediately after use.

## Issues or concerns

- **`uv.lock` is locally modified** (dependency resolution drift, e.g. `greenlet`
  3.5.5 → 3.1.1) and was already dirty before I touched anything — I did not cause
  it and left it untouched/uncommitted, since it's unrelated to this task and outside
  my scope. Flagging so it isn't mistaken for something this task introduced.
- The rsync command specified in my instructions doesn't exclude `.venv` or
  `__pycache__`; see the sync note above.
- The Caddy snippet deviates from the brief's literal draft (see rationale above). If
  you'd rather keep the `path_regexp`/`rewrite` form for consistency with how you
  imagined it, say so and I'll swap it back — but I'd recommend keeping `try_files`
  for the secret-exposure and double-extension reasons given.

## Fix report — post-review corrections

Review came back approved with two findings to fix before completion.

**Finding 1 (Important, plan-mandated) — runbook dropped the final commit step.**
The brief's Step 12 commits `tests/test_parse_real.py` and
`tests/fixtures/real_profile.json` (the artefacts from the capture step), but my
runbook told the operator to write and run them and then stopped. Since
`deploy/run.sh` and `deploy/Caddyfile.snippet` were already committed separately in
`4e74eab`, the brief's single bundled commit can no longer happen as originally
written. Fixed by adding **step 9** to the handover runbook above: `git add
tests/test_parse_real.py tests/fixtures/real_profile.json && git commit -m "🚀 chore:
real-capture parser fixture"`, with its own success criterion.

**Finding 2 (escalated from Minor) — `run.sh` slept before checking config.**
The jitter `sleep $((RANDOM % 1800))` ran before the `config.toml` existence guard,
so a first-time misconfiguration would only surface after up to a 30-minute wait —
a real cost during the exact first-time setup this project is about to go through.
Fixed by moving the guard above the sleep in `deploy/run.sh`; the jitter still runs,
unchanged, immediately before `docker run`, for the success path.

Diff (`deploy/run.sh`): the `if [ ! -f "$DATA/config.toml" ]; then ... fi` block now
comes immediately after the `DATA`/`PUBLISH` assignments, and `sleep $((RANDOM %
1800))` moved to just before the `--shm-size` comment / `docker run` call. Comment
wording updated to say "before the jitter sleep" instead of "in the Unraid script
log instead."

**Re-verification:**
```
$ bash -n ~/Projects/instascrap/deploy/run.sh && echo "local bash -n OK"
local bash -n OK

$ rsync -av deploy/run.sh tower:/mnt/user/appdata/instascrap-src/deploy/run.sh
sending incremental file list
run.sh
sent 1.262 bytes  received 47 bytes  2.618,00 bytes/sec
total size is 1.157  speedup is 0,88

$ ssh tower "bash -n /mnt/user/appdata/instascrap-src/deploy/run.sh && echo 'remote bash -n OK'"
remote bash -n OK
```

Re-exercised the guard-before-sleep ordering against a scratch directory (no config
present, mirroring tower's actual current state):
```
$ time bash -c '
DATA=".../fake-data2"
if [ ! -f "$DATA/config.toml" ]; then
  echo "missing $DATA/config.toml -- see config.example.toml in the repo" >&2
  exit 1
fi
sleep $((RANDOM % 1800))
echo "should not reach here"
'
missing .../fake-data2/config.toml -- see config.example.toml in the repo
bash -c   0,01s user 0,00s system 72% cpu 0,014 total
exit code: 1
```
Confirms the failure is now immediate (~0.01s) rather than after up to 30 minutes of
jitter, and that the "should not reach here" line is never printed.

The third finding (rsync landing `.venv`/`__pycache__` on tower) was attributed
to the reviewer's own instructions, not mine, and needs no action from me.

Committed as a follow-up commit (see Files changed).
