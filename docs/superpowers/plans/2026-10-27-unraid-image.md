# instascrap — image Unraid partageable : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** transformer instascrap en conteneur autonome, longue durée, configuré par
variables d'environnement, qui planifie ses passages, sert ses flux, gère seul la
reconnexion Instagram et alerte sur Discord.

**Architecture:** un processus Python `--serve` (derrière `tini`) remplace le cron,
`run.sh` et `login.sh` : il valide la config, supervise un Caddy enfant qui sert
`/data/public`, régénère les flux, puis alterne attente du créneau suivant et
passage. Une déconnexion Instagram devient une `LoggedOutError` distincte du blocage,
qui fait basculer en mode connexion (Xvfb + x11vnc protégé + noVNC, ouverts
seulement pendant la connexion). Les alertes passent par un webhook Discord.

**Tech Stack:** Python 3.12, Playwright 1.49 (Chrome), feedgen, Caddy 2 (binaire
officiel), tini, Xvfb/x11vnc/noVNC, GitHub Actions → ghcr.io.

**Spec:** `docs/superpowers/specs/2026-10-27-unraid-image-design.md`

## Global Constraints

- Aucun changement de comportement envers Instagram : ordre de visite, pauses,
  détection de grille, backoff 24 h puis 72 h (`store.FIRST_BACKOFF`,
  `REPEAT_BACKOFF`) inchangés.
- Une déconnexion n'entraîne **jamais** de backoff ; un blocage n'ouvre **jamais**
  l'écran de connexion. En cas de signal ambigu, le blocage l'emporte.
- Aucun Chrome ni écran de connexion ne démarre tant que `store.blocked_until()` est
  dans le futur.
- Langue : anglais pour les logs, les alertes, le template, le README. Commentaires
  de code en anglais (règle existante du projet).
- Variables (noms exacts) : `ACCOUNTS`, `PUBLIC_URL`, `VNC_PASSWORD`, `SCHEDULE`
  (défaut `08:00,14:00,21:00`), `ACCOUNTS_PER_SESSION` (7), `FEED_ITEMS` (50),
  `MIN_GAP_SECONDS` (25), `MAX_GAP_SECONDS` (90), `JITTER_MINUTES` (30),
  `FEED_SECRET` (vide), `DISCORD_WEBHOOK` (vide), `PUID` (99), `PGID` (100). Lus
  aussi : `TZ` (défaut UTC), `HOST_HOSTNAME` (optionnel, injecté par Unraid).
- Disposition : `/data` unique ; flux publiés dans `/data/public/<préfixe>/` ;
  secret généré dans `/data/feed_secret`.
- Ports conteneur : `8080` (Caddy, HTTP, sans `browse`, `admin off`), `6080` (noVNC,
  ouvert uniquement pendant une connexion).
- Image : `ghcr.io/imthai/instascrap`, `linux/amd64`, construite sur tag `v*`.
- Git : l'utilisateur interdit tout commit sans demande explicite. Les étapes
  « Commit » ne s'exécutent qu'avec son accord ; à défaut, laisser les changements
  en place et passer à la tâche suivante.
- Tower : les URL publiques existantes (`https://feeds.example.com/3f9a1c0b7d2e4a68/...`)
  doivent rester identiques après migration.

## Review Focus

1. **`ACCOUNTS` saisi à la main** (`"@nasagoddard, map_of_europe,,"`, doublons,
   espaces) : `@` retiré, vides ignorés, doublons supprimés dans l'ordre, puis
   validation de charset existante. Test : Task 1.
2. **`VNC_PASSWORD` de plus de 8 caractères** : l'authentification VNC n'en garde
   que 8 ; le démarrage logge un avertissement explicite. Vide → refus de démarrer.
   Test : Task 1.
3. **`SCHEDULE` mal formé** (`8h`, `25:00`, `08:00;14:00`) : erreur claire au
   démarrage, pas de boucle muette ; `8:00` accepté. Test : Task 1.
4. **Changement d'heure et créneau proche de minuit** : `next_slot` travaille en
   datetimes conscients du fuseau ; le 25/10/2026 (passage à l'heure d'hiver à
   Paris) un créneau `08:00` tombe bien à 08:00 locale. Test : Task 3.
5. **Profil Chrome verrouillé après un arrêt brutal** (`SingletonLock`,
   `SingletonSocket`, `SingletonCookie` laissés dans `/data/profile` par un
   conteneur tué) : supprimés au démarrage de `--serve`, avant tout lancement de
   Chrome. Test : Task 5.

---

## File Structure

| Fichier | Rôle |
|:--|:--|
| `src/instascrap/config.py` | **Modifié.** `load_config(env)` depuis les variables ; secret, horaires, comptes |
| `src/instascrap/parse.py` | **Modifié.** `refusal_kind()` : déconnecté / bloqué |
| `src/instascrap/browser.py` | **Modifié.** `LoggedOutError`, `raise_for_refusal()`, `Session.cookies()`/`current_url()` |
| `src/instascrap/store.py` | **Modifié.** état `login` dans `state.json` |
| `src/instascrap/alerts.py` | **Créé.** envoi Discord ; remplace `notify.py` |
| `src/instascrap/schedule.py` | **Créé.** `next_slot()` pur |
| `src/instascrap/login.py` | **Créé.** écran de connexion (Xvfb/x11vnc/noVNC) et attente de session |
| `src/instascrap/daemon.py` | **Créé.** boucle `--serve`, supervision de Caddy, nettoyage du profil |
| `src/instascrap/main.py` | **Modifié.** CLI sans `--config`, alertes anglaises, `--serve`, `--login` automatique |
| `docker/Caddyfile`, `docker/entrypoint.sh` | **Créés.** service HTTP ; PUID/PGID puis `exec` |
| `Dockerfile` | **Modifié.** tini, Caddy, entrypoint, `CMD ["--serve"]` |
| `unraid/instascrap.xml` | **Créé.** template |
| `README.md` | **Créé.** doc anglaise |
| `.github/workflows/release.yml` | **Créé.** build + push ghcr sur tag |
| `deploy/*`, `docs/DEPLOYMENT.md`, `config.example.toml`, `src/instascrap/notify.py`, `tests/test_notify.py` | **Supprimés** (Task 8, après migration) |

---

### Task 1 : configuration par variables d'environnement

**Files:**
- Modify: `src/instascrap/config.py`
- Modify: `tests/test_config.py` (réécrit : les tests TOML disparaissent)
- Modify: `tests/test_main.py`, `tests/test_feed.py`, `tests/test_browser_contract.py`
  (seulement si les constructeurs `Config(...)` cassent — voir ci-dessous)

**Interfaces:**
- Produces :
  - `Config` garde ses champs actuels (`publish_dir` vaut désormais
    `data_dir / "public"`) et gagne, **avec valeurs par défaut** pour ne pas casser
    les constructeurs des tests existants : `schedule: tuple[datetime.time, ...] = (time(8), time(14), time(21))`,
    `jitter_minutes: int = 30`, `vnc_password: str = ""`,
    `discord_webhook: str | None = None`, `host_hostname: str | None = None`,
    `tz: str = "UTC"`.
  - `load_config(env: Mapping[str, str], data_dir: Path = Path("/data")) -> Config`
  - `parse_accounts(raw: str) -> tuple[str, ...]`
  - `parse_schedule(raw: str) -> tuple[time, ...]` (triés, dédoublonnés)
  - `resolve_feed_secret(value: str, data_dir: Path) -> str`
  - `class ConfigError(ValueError)` — message destiné à l'utilisateur, en anglais,
    nommant la variable fautive.

- [ ] **Step 1 : écrire les tests qui échouent** dans `tests/test_config.py`

```python
BASE = {"ACCOUNTS": "alice,bob", "PUBLIC_URL": "https://feeds.example.com/",
        "VNC_PASSWORD": "secret12"}

def test_minimal_env_gives_spec_defaults(tmp_path):
    cfg = load_config(BASE, data_dir=tmp_path)
    assert cfg.accounts == ("alice", "bob")
    assert cfg.public_base_url == "https://feeds.example.com"
    assert cfg.publish_dir == tmp_path / "public"
    assert (cfg.accounts_per_session, cfg.feed_items) == (7, 50)
    assert (cfg.min_gap_seconds, cfg.max_gap_seconds, cfg.jitter_minutes) == (25, 90, 30)
    assert cfg.schedule == (time(8), time(14), time(21))
    assert cfg.discord_webhook is None and cfg.tz == "UTC"

def test_accounts_are_cleaned():
    assert parse_accounts(" @nasagoddard, map_of_europe,,nasagoddard ") == ("nasagoddard", "map_of_europe")

@pytest.mark.parametrize("raw", ["", " , ", "bad name", ".."])
def test_bad_accounts_are_rejected(raw):
    with pytest.raises(ConfigError, match="ACCOUNTS"):
        parse_accounts(raw)

def test_schedule_accepts_single_digit_hour():
    assert parse_schedule("21:00, 8:00") == (time(8), time(21))

@pytest.mark.parametrize("raw", ["8h", "25:00", "08:00;14:00", ""])
def test_bad_schedule_is_rejected(raw):
    with pytest.raises(ConfigError, match="SCHEDULE"):
        parse_schedule(raw)

@pytest.mark.parametrize("missing", ["ACCOUNTS", "PUBLIC_URL", "VNC_PASSWORD"])
def test_required_variables(tmp_path, missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_config(env, data_dir=tmp_path)

@pytest.mark.parametrize("url", ["feeds.example.com", "ftp://x", "https://"])
def test_public_url_needs_http_scheme_and_host(tmp_path, url):
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        load_config({**BASE, "PUBLIC_URL": url}, data_dir=tmp_path)

def test_long_vnc_password_warns(tmp_path, caplog):
    load_config({**BASE, "VNC_PASSWORD": "123456789"}, data_dir=tmp_path)
    assert "only the first 8 characters" in caplog.text

def test_feed_secret_is_generated_once_and_persisted(tmp_path):
    first = resolve_feed_secret("", tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    assert resolve_feed_secret("", tmp_path) == first
    assert (tmp_path / "feed_secret").read_text().strip() == first

def test_explicit_feed_secret_wins_and_is_validated(tmp_path):
    (tmp_path / "feed_secret").write_text("aaaaaaaaaaaaaaaa")
    assert resolve_feed_secret("3f9a1c0b7d2e4a68", tmp_path) == "3f9a1c0b7d2e4a68"
    with pytest.raises(ConfigError, match="FEED_SECRET"):
        resolve_feed_secret("NOTHEX", tmp_path)

def test_gap_bounds_are_checked(tmp_path):
    with pytest.raises(ConfigError, match="MIN_GAP_SECONDS"):
        load_config({**BASE, "MIN_GAP_SECONDS": "100"}, data_dir=tmp_path)

def test_non_integer_is_named(tmp_path):
    with pytest.raises(ConfigError, match="FEED_ITEMS"):
        load_config({**BASE, "FEED_ITEMS": "lots"}, data_dir=tmp_path)

def test_empty_discord_webhook_means_none_and_host_is_read(tmp_path):
    cfg = load_config({**BASE, "DISCORD_WEBHOOK": " ", "HOST_HOSTNAME": "Tower",
                       "TZ": "Europe/Paris"}, data_dir=tmp_path)
    assert cfg.discord_webhook is None
    assert (cfg.host_hostname, cfg.tz) == ("Tower", "Europe/Paris")
```

- [ ] **Step 2 :** `uv run pytest tests/test_config.py -q` → FAIL (`load_config` n'accepte pas un mapping).

- [ ] **Step 3 : implémenter** dans `config.py`. Supprimer le chargement TOML.
  Réutiliser `ACCOUNT_RE` et `PREFIX_RE`. Un `TZ` inconnu de `zoneinfo` →
  `ConfigError("TZ ...")`. `resolve_feed_secret` écrit le fichier de façon
  atomique (tmp + `os.replace`). Mettre à jour la docstring du module (le préfixe
  reste un secret partagé). Si des tests existants construisent `Config(...)` avec
  `publish_dir=...`, ils continuent de marcher : le champ reste un argument.
  Dans `main.py`, supprimer l'option `--config` et appeler
  `load_config(os.environ)` ; le reste de la CLI change à la Task 5.

- [ ] **Step 4 :** `uv run pytest -q` → tout passe. Un test de `test_main.py` qui
  passait `--config` à `main()` est adapté par `monkeypatch.setattr(os, "environ", {...})`.

- [ ] **Step 5 : commit** `feat: read configuration from environment variables`

---

### Task 2 : distinguer « déconnecté » et « bloqué »

**Files:**
- Modify: `src/instascrap/parse.py`, `src/instascrap/browser.py`, `src/instascrap/store.py`, `src/instascrap/main.py`
- Test: `tests/test_block_detection.py`, `tests/test_browser_contract.py`, `tests/test_store_state.py`, `tests/test_main.py`

**Interfaces:**
- Produces :
  - `parse.refusal_kind(status: int, payload: dict | None, url: str) -> Literal["blocked", "logged_out"] | None`
    — `"blocked"` si 401/429 ou un marqueur (`feedback_required`, `challenge_required`)
    est présent, **même** sur une URL de connexion ; sinon `"logged_out"` si l'URL
    contient `/accounts/login` ou `/challenge` ; sinon `None`. `detect_block` et
    `is_bare_login_redirect` restent inchangés (leurs tests aussi).
  - `browser.LoggedOutError(BlockedError)` — sous-classe : tout `except BlockedError`
    existant continue d'arrêter la session sur-le-champ.
  - `browser.raise_for_refusal(status, payload, url) -> None` — lève
    `LoggedOutError` ou `BlockedError` avec le motif de `detect_block`.
    `collect_payload` et `Session.fetch_media` l'utilisent à la place de leur appel
    direct à `detect_block`.
  - `Store.login_required() -> str | None` (motif), `Store.require_login(reason: str, now: datetime) -> None`,
    `Store.clear_login() -> None`, `Store.last_login_alert() -> datetime | None`,
    `Store.mark_login_alert(now: datetime) -> None`. Clé `"login"` ajoutée à
    `_DEFAULT_STATE` (`None` par défaut) : un `state.json` existant sans la clé se lit
    comme « pas de connexion requise ».
  - `SessionResult.logged_out: str | None = None`. `run_session` attrape
    `LoggedOutError` **avant** `BlockedError` à chacun de ses trois points de capture :
    remplit `logged_out`, appelle `store.require_login(...)`, **n'appelle pas**
    `record_block`, et retourne. `had_expired_block` n'est pas soldé dans ce cas.

- [ ] **Step 1 : tests qui échouent**

```python
# test_block_detection.py
def test_login_redirect_is_logged_out():
    assert refusal_kind(200, None, "https://www.instagram.com/accounts/login/?next=/") == "logged_out"
def test_challenge_path_is_logged_out():
    assert refusal_kind(200, None, "https://www.instagram.com/challenge/abc/") == "logged_out"
def test_rate_limit_wins_over_login_url():
    assert refusal_kind(429, None, "https://www.instagram.com/accounts/login/") == "blocked"
def test_marker_wins_over_login_url():
    assert refusal_kind(400, {"message": "challenge_required"},
                        "https://www.instagram.com/accounts/login/") == "blocked"
def test_ordinary_response_is_no_refusal():
    assert refusal_kind(200, {"status": "ok"}, "https://www.instagram.com/api/graphql") is None

# test_browser_contract.py
def test_collect_payload_raises_logged_out_on_login_redirect():
    with pytest.raises(LoggedOutError):
        collect_payload(200, "https://www.instagram.com/accounts/login/", None)
def test_logged_out_is_still_a_blocked_error():
    assert issubclass(LoggedOutError, BlockedError)

# test_store_state.py
def test_login_state_round_trip(tmp_path):
    store = Store(tmp_path)
    assert store.login_required() is None
    store.require_login("redirected to /accounts/login", NOW)
    assert store.login_required() == "redirected to /accounts/login"
    store.mark_login_alert(NOW)
    assert store.last_login_alert() == NOW
    store.clear_login()
    assert store.login_required() is None and store.last_login_alert() is None

# test_main.py — FakeSession gagne un paramètre logout_on=account qui lève LoggedOutError
def test_logout_requires_login_without_backoff(tmp_path):
    store = Store(tmp_path / "data")
    result = run_session(cfg_for(tmp_path), store, FakeSession(logout_on="alice"), NOW)
    assert result.logged_out and result.blocked is None
    assert store.blocked_until() is None
    assert store.login_required()
```

- [ ] **Step 2 :** `uv run pytest -q` → les nouveaux tests échouent.
- [ ] **Step 3 : implémenter** selon l'interface ci-dessus.
- [ ] **Step 4 :** `uv run pytest -q` → tout passe, y compris les tests existants
  de `is_bare_login_redirect` et de `expect_login_redirect`.
- [ ] **Step 5 : commit** `feat: tell a logged-out session apart from a block`

---

### Task 3 : planification et alertes Discord

**Files:**
- Create: `src/instascrap/schedule.py`, `src/instascrap/alerts.py`
- Modify: `src/instascrap/main.py` (remplace tous les `notify(...)` français)
- Test: `tests/test_schedule.py`, `tests/test_alerts.py`, `tests/test_main.py`

**Interfaces:**
- Produces :
  - `schedule.next_slot(now: datetime, slots: tuple[time, ...], tz: ZoneInfo, jitter: timedelta, rng: random.Random) -> datetime`
    — premier créneau local **strictement postérieur** à `now`, converti en UTC,
    plus `rng.uniform(0, jitter)`. Pas de rattrapage des créneaux passés.
  - `alerts.Level = Literal["info", "warning", "alert"]`
  - `alerts.Alerter(webhook: str | None, post: Callable[[str, dict], None] = _post_json)`
    avec `send(level: Level, title: str, message: str) -> None`. Toujours logger
    (`warning` pour warning/alert, `info` sinon). Si webhook : POST d'un embed
    Discord `{"embeds": [{"title", "description", "color"}]}`, couleurs info
    `0x2ECC71`, warning `0xF1C40F`, alert `0xE74C3C`. Toute exception de `post` est
    loggée et avalée. `_post_json` utilise `urllib.request`, timeout 10 s.
  - `main.run_once(cfg: Config, store: Store, alerter: Alerter, now: datetime, session_factory=Session) -> SessionResult`
    — remplace `_run` : même logique (y compris le refus de lancer Chrome pendant un
    backoff), mais émet les alertes du tableau ci-dessous au lieu de `notify` et
    de `last_run.json`. `last_run.json` est conservé (écrit comme aujourd'hui)
    pour le diagnostic manuel.

Textes exacts (copie fixée) :

| Condition | Niveau | Titre | Message |
|:--|:--|:--|:--|
| blocage | alert | `Instagram is throttling` | `{reason}. Pausing until {until:%Y-%m-%d %H:%M %Z}.` |
| tous les profils visités sans grille | alert | `Capture broken` | `None of the {n} visited profiles returned its grid. Instagram probably changed something.` |
| une partie sans grille | warning | `Missing grids` | `No grid for: {accounts}.` |
| échecs isolés | warning | `Partial failures` | `{n} failure(s) on {visited} account(s), {new} new post(s). First: {first}` |
| silence (> 7 j) | alert | `No new posts` | `Nothing new for {days} days across all accounts. The parser may be broken.` |
| exception d'un passage | alert | `Run crashed` | `{exc}` |

L'heure de `{until}` est affichée dans le fuseau `cfg.tz`.

- [ ] **Step 1 : tests qui échouent**

```python
PARIS = ZoneInfo("Europe/Paris")
def test_next_slot_same_day():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8), time(14)), PARIS, timedelta(0), Random(1)) == datetime(2026, 10, 20, 14, 0, tzinfo=PARIS)
def test_next_slot_rolls_to_tomorrow_without_catch_up():
    now = datetime(2026, 10, 20, 22, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8), time(14)), PARIS, timedelta(0), Random(1)) == datetime(2026, 10, 21, 8, 0, tzinfo=PARIS)
def test_next_slot_is_strictly_after_now():
    now = datetime(2026, 10, 20, 8, 0, tzinfo=PARIS)
    assert next_slot(now, (time(8),), PARIS, timedelta(0), Random(1)).day == 21
def test_next_slot_across_dst_change():
    now = datetime(2026, 10, 24, 22, 0, tzinfo=PARIS)   # winter time starts 25/10 03:00
    slot = next_slot(now, (time(8),), PARIS, timedelta(0), Random(1))
    assert slot.astimezone(PARIS).hour == 8 and slot.utcoffset() is not None
def test_jitter_stays_within_bounds():
    now = datetime(2026, 10, 20, 9, 0, tzinfo=PARIS)
    base = datetime(2026, 10, 20, 14, 0, tzinfo=PARIS)
    for seed in range(50):
        delta = next_slot(now, (time(14),), PARIS, timedelta(minutes=30), Random(seed)) - base
        assert timedelta(0) <= delta <= timedelta(minutes=30)

def test_alerter_posts_colored_embed():
    calls = []
    Alerter("https://discord.test/hook", post=lambda url, body: calls.append((url, body))).send("alert", "T", "M")
    assert calls == [("https://discord.test/hook", {"embeds": [{"title": "T", "description": "M", "color": 0xE74C3C}]})]
def test_alerter_without_webhook_only_logs(caplog):
    Alerter(None).send("warning", "T", "M")
    assert "T" in caplog.text
def test_alerter_swallows_post_failure(caplog):
    def boom(url, body): raise OSError("down")
    Alerter("https://discord.test/hook", post=boom).send("info", "T", "M")
    assert "down" in caplog.text
```

  Dans `test_main.py` : remplacer les deux `monkeypatch.setattr(main_module, "notify", ...)`
  par un `Alerter` factice qui enregistre `(level, title)`, et ajouter
  `test_all_grids_missing_sends_capture_broken` (titre `Capture broken`, niveau `alert`)
  et `test_block_alert_names_resume_time` (titre `Instagram is throttling`).

- [ ] **Step 2 :** `uv run pytest -q` → échecs attendus.
- [ ] **Step 3 : implémenter** `schedule.py`, `alerts.py`, `run_once`. Construire
  le créneau avec `datetime.combine(date, slot, tzinfo=tz)` (zoneinfo gère l'heure
  d'été). Supprimer l'import de `notify` dans `main.py`.
- [ ] **Step 4 :** `uv run pytest -q` → tout passe.
- [ ] **Step 5 : commit** `feat: schedule runs in-process and alert through Discord`

---

### Task 4 : écran de connexion automatique

**Files:**
- Create: `src/instascrap/login.py`
- Modify: `src/instascrap/browser.py` (accesseurs), `src/instascrap/main.py` (`--login`)
- Test: `tests/test_login.py`

**Interfaces:**
- Consumes : `Store.login_required/require_login/clear_login/last_login_alert/mark_login_alert`,
  `Alerter.send`, `Session(cfg, profile_dir, headless=False)`,
  `Session.expect_login_redirect()`, `Session.raise_if_blocked()`, `LoggedOutError`.
- Produces :
  - `Session.cookie_names() -> set[str]` et `Session.current_url() -> str` (lecture
    seule, aucune navigation).
  - `login.is_logged_in(cookie_names: set[str], url: str) -> bool` — `"sessionid"`
    présent **et** URL sans `/accounts/login` ni `/challenge`.
  - `login.vnc_url(cfg: Config) -> str` — `http://{cfg.host_hostname}:6080/vnc.html`,
    ou `http://<your-server-ip>:6080/vnc.html` si `host_hostname` est `None`.
  - `login.LoginScreen(password: str)` — context manager : écrit le mot de passe
    avec `x11vnc -storepasswd {password} {tmpfile}` dans un répertoire temporaire
    `0700`, lance Xvfb `:99` (`1440x900x24`), `x11vnc -display :99 -rfbauth {tmpfile} -forever -shared -localhost`,
    `websockify --web=/usr/share/novnc 6080 localhost:5900`, avec les mêmes attentes
    bornées que `deploy/login.sh` (`wait_for` : socket X, port 5900, port 6080, 15 s
    chacune). Exporte `DISPLAY=:99` pour Chrome. `__exit__` termine les trois
    processus (SIGTERM, puis SIGKILL après 5 s) et supprime le fichier de mot de passe.
  - `login.run_login(cfg: Config, store: Store, alerter: Alerter, *, clock=datetime.now, sleep=time.sleep, screen_factory=LoginScreen, session_factory=Session, poll_s: float = 5.0) -> None`
    — déroulé :
    1. si `store.blocked_until()` est futur → retour immédiat (le démon attendra) ;
    2. ouvre l'écran et une `Session` visible, navigue vers
       `https://www.instagram.com/accounts/login/` sous `expect_login_redirect()` ;
    3. alerte `alert` titre `Instagram login needed`, message
       `{"No Instagram session yet" | "The Instagram session expired"}. Log in at {vnc_url} (the server's IP works too). Never expose port 6080 through your reverse proxy.`
       puis `store.mark_login_alert(now)` ;
    4. boucle : `session.raise_if_blocked()` ; si `is_logged_in(...)` → sortir ;
       si `now - last_login_alert >= 24 h` → renvoyer la même alerte ; `sleep(poll_s)` ;
    5. succès : `store.clear_login()`, alerte `info` titre `Logged in`, message
       `Instagram session restored. Runs resume at the next scheduled time.` ;
    6. `BlockedError` pendant la boucle → `store.record_block(...)`, alerte de
       blocage (texte de la Task 3), sortie ; la connexion reste requise.
    Le motif « No session yet » est utilisé quand `store.login_required()` vaut
    `"first run"`.
  - `main --login` appelle `run_login` (plus d'`input()`), en forçant
    `store.require_login("manual login", now)` si aucun motif n'est enregistré.

- [ ] **Step 1 : tests qui échouent** (`tests/test_login.py`, avec `FakeScreen`
  context manager no-op et `FakeLoginSession` qui renvoie une séquence d'états)

```python
def test_is_logged_in():
    assert is_logged_in({"sessionid", "csrftoken"}, "https://www.instagram.com/")
    assert not is_logged_in({"csrftoken"}, "https://www.instagram.com/")
    assert not is_logged_in({"sessionid"}, "https://www.instagram.com/challenge/x/")

def test_vnc_url_uses_unraid_hostname(tmp_path):
    assert vnc_url(cfg_with(tmp_path, host_hostname="Tower")) == "http://Tower:6080/vnc.html"
    assert "<your-server-ip>" in vnc_url(cfg_with(tmp_path, host_hostname=None))

def test_login_success_clears_state_and_announces(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("first run", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False, False, True]))
    assert store.login_required() is None
    assert [t for _, t in alerts] == ["Instagram login needed", "Logged in"]

def test_reminder_once_a_day(tmp_path):
    store, alerts = Store(tmp_path), []
    store.require_login("expired", NOW)
    ticks = iter(NOW + timedelta(hours=13 * i) for i in range(10))
    run_login(cfg_with(tmp_path), store, recording_alerter(alerts), clock=lambda: next(ticks),
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False, False, False, False, True]))
    # alerts at t=0, then t=26h and t=52h (each ≥ 24 h after the previous one)
    assert [t for _, t in alerts].count("Instagram login needed") == 3

def test_never_opens_during_backoff(tmp_path):
    store = Store(tmp_path); store.require_login("x", NOW); store.record_block("HTTP 429", NOW)
    opened = []
    run_login(cfg_with(tmp_path), store, recording_alerter([]), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=lambda pw: opened.append(pw) or FakeScreen(pw),
              session_factory=fake_session_states([True]))
    assert opened == []

def test_block_during_login_records_backoff_and_keeps_login_required(tmp_path):
    store = Store(tmp_path); store.require_login("expired", NOW)
    run_login(cfg_with(tmp_path), store, recording_alerter([]), clock=lambda: NOW,
              sleep=lambda s: None, screen_factory=FakeScreen,
              session_factory=fake_session_states([False, BlockedError("challenge_required")]))
    assert store.blocked_until() is not None and store.login_required() == "expired"
```

- [ ] **Step 2 :** `uv run pytest tests/test_login.py -q` → FAIL.
- [ ] **Step 3 : implémenter.** `LoginScreen` n'est pas testé unitairement (il lance
  de vrais binaires) : il est vérifié au smoke test de la Task 6.
- [ ] **Step 4 :** `uv run pytest -q` → tout passe.
- [ ] **Step 5 : commit** `feat: open the login screen on its own when the session expires`

---

### Task 5 : démon `--serve` et supervision de Caddy

**Files:**
- Create: `src/instascrap/daemon.py`, `docker/Caddyfile`
- Modify: `src/instascrap/main.py` (CLI)
- Test: `tests/test_daemon.py`, `tests/test_main.py`

**Interfaces:**
- Consumes : `load_config`, `resolve_feed_secret`, `publish`, `run_once`, `run_login`,
  `next_slot`, `Alerter`, `Store`.
- Produces :
  - `daemon.clear_profile_locks(profile_dir: Path) -> list[str]` — supprime
    `SingletonLock`, `SingletonSocket`, `SingletonCookie` (ce sont souvent des liens
    symboliques cassés : utiliser `os.path.lexists`/`unlink`), renvoie les noms supprimés.
  - `daemon.initial_login_reason(profile_dir: Path, store: Store) -> str | None` —
    `store.login_required()` s'il existe ; sinon `"first run"` si `profile_dir`
    n'existe pas ou est vide ; sinon `None` (cas de tower : profil existant,
    pas de motif → pas de reconnexion).
  - `daemon.CaddySupervisor(cmd: list[str], popen=subprocess.Popen, clock=time.monotonic)` avec `start()`, `poll() -> None`
    (relance si le processus est mort, log `caddy exited with {code}, restarting`,
    au plus une relance toutes les 10 s) et `stop()`.
  - `daemon.serve(cfg: Config, *, clock=..., sleep=..., rng=random.Random(), caddy=..., login=run_login, run=run_once, max_iterations: int | None = None) -> None`
    — ordre au démarrage : `clear_profile_locks`, `caddy.start()`, `publish(store, cfg)`,
    log `Feeds index: {public_base_url}/{public_prefix}/index.html`, puis si
    `initial_login_reason` → `store.require_login(reason)`. Boucle :
    si `store.login_required()` → `login(...)` puis, si toujours requis, attendre
    `min(1 h, fin du backoff)` avant de réessayer ; sinon calculer `next_slot`,
    dormir par tranches de ≤ 60 s en appelant `caddy.poll()`, puis `run(...)`.
    Pendant un backoff, le créneau est sauté avec le log `blocked until {until}, skipping`.
    SIGTERM/SIGINT : arrêt propre (Caddy arrêté, pas de traceback).
  - `docker/Caddyfile` :

```
{
	admin off
	persist_config off
}
:8080 {
	root * /data/public
	header {
		X-Robots-Tag "noindex, nofollow"
		Referrer-Policy "no-referrer"
		-Server
	}
	file_server
}
```

  - CLI (`--config` déjà retiré à la Task 1) : modes `--serve` (défaut de l'image), `--run`
    (un passage immédiat), `--login`, `--capture ACCOUNT`, `--republish`. Tous
    chargent `load_config(os.environ)`. Une `ConfigError` → message sur stderr,
    code 1, sans traceback.

- [ ] **Step 1 : tests qui échouent**

```python
def test_clear_profile_locks_removes_dangling_symlinks(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("old-host-123")
    (tmp_path / "SingletonCookie").symlink_to("456")
    (tmp_path / "Default").mkdir()
    assert sorted(clear_profile_locks(tmp_path)) == ["SingletonCookie", "SingletonLock"]
    assert (tmp_path / "Default").exists()

def test_initial_login_reason(tmp_path):
    store = Store(tmp_path / "data")
    assert initial_login_reason(tmp_path / "missing", store) == "first run"
    (tmp_path / "profile").mkdir(); (tmp_path / "profile" / "Default").mkdir()
    assert initial_login_reason(tmp_path / "profile", store) is None

# Common harness: calls = []; fakes for caddy/login/run/publish append their name;
# clock/sleep are fakes; max_iterations bounds the loop.
def test_serve_publishes_before_first_run_and_logs_index(tmp_path, caplog):
    serve(cfg_with(tmp_path), **fakes(calls), max_iterations=1)
    assert calls[:3] == ["caddy.start", "publish", "run"]
    assert "Feeds index: https://example.test/0123456789abcdef/index.html" in caplog.text

def test_serve_logs_in_first_when_required(tmp_path):
    # empty profile dir → "first run"
    serve(cfg_with(tmp_path), **fakes(calls), max_iterations=2)
    assert calls.index("login") < calls.index("run")

def test_serve_skips_slot_during_backoff(tmp_path, caplog):
    Store(cfg_with(tmp_path).data_dir).record_block("HTTP 429", NOW)
    serve(cfg_with(tmp_path), **fakes(calls, now=NOW), max_iterations=1)
    assert "run" not in calls and "skipping" in caplog.text

def test_caddy_supervisor_restarts_dead_process():
    procs = [FakePopen(exit_code=1), FakePopen(exit_code=None)]
    sup = CaddySupervisor(["caddy"], popen=lambda cmd: procs.pop(0), clock=fake_clock_step(11))
    sup.start(); sup.poll()
    assert procs == []   # second process spawned after the first died

def test_config_error_exits_1_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {})
    assert main_module.main(["--run"]) == 1
    assert "ACCOUNTS" in capsys.readouterr().err
```

- [ ] **Step 2 :** `uv run pytest -q` → FAIL.
- [ ] **Step 3 : implémenter.**
- [ ] **Step 4 :** `uv run pytest -q` → tout passe.
- [ ] **Step 5 : commit** `feat: long-running --serve mode with a supervised Caddy`

---

### Task 6 : image Docker

**Files:**
- Create: `docker/entrypoint.sh`
- Modify: `Dockerfile`, `.dockerignore`

**Interfaces:**
- Consumes : `docker/Caddyfile`, `python3 -m instascrap --serve`.
- Produces : une image dont `ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]`
  et `CMD ["--serve"]`, `EXPOSE 8080 6080`, `VOLUME /data`.

Contenu requis :
- `Dockerfile` : garder la base `mcr.microsoft.com/playwright:v1.49.0-noble` et
  l'installation de Chrome ; ajouter `tini` via apt ; copier le binaire Caddy depuis
  `caddy:2` (`COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy`) ;
  `COPY docker/Caddyfile /etc/caddy/Caddyfile` ; `mkdir -m 1777 -p /tmp/.X11-unix` ;
  `ENV HOME=/data/home XDG_CONFIG_HOME=/tmp/xdg XDG_DATA_HOME=/tmp/xdg`. Conserver les
  commentaires existants sur l'épinglage de Playwright.
- `entrypoint.sh` (bash, `set -euo pipefail`) : lire `PUID`/`PGID` (99/100) ;
  créer `/data/home` ; si le propriétaire de `/data` diffère, `chown -R PUID:PGID /data`
  avec un log (cas des données root de tower) ; puis
  `exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python3 -m instascrap "$@"`.
  Chrome sous Playwright tourne déjà avec `--no-sandbox` par défaut : rien à changer.
- Le démon lance Caddy avec `caddy run --config /etc/caddy/Caddyfile --adapter caddyfile`.

- [ ] **Step 1 :** synchroniser les sources sur tower (même commande `rsync` que
  d'habitude, en excluant `.git .venv __pycache__ .pytest_cache`) et construire
  là-bas : `docker build -t instascrap:next .` (l'image est `amd64`, tower aussi).
- [ ] **Step 2 : smoke test sur tower**, sans toucher au déploiement en cours,
  avec un `/data` jetable et des ports hôtes libres :

```bash
mkdir -p /tmp/is-smoke && docker run -d --name is-smoke -v /tmp/is-smoke:/data \
  -e ACCOUNTS=nasagoddard -e PUBLIC_URL=http://tower:18080 -e VNC_PASSWORD=smoketst \
  -e TZ=Europe/Paris -e HOST_HOSTNAME=Tower -p 18080:8080 -p 16080:6080 instascrap:next
```

  Attendu :
  - `docker logs is-smoke` contient `Feeds index: http://tower:18080/<16 hex>/index.html`
    et l'alerte `Instagram login needed` avec `http://Tower:6080/vnc.html` ;
  - `curl -s -o /dev/null -w '%{http_code}' http://tower:18080/` → `404` ;
  - l'URL de l'index → `200` ;
  - `curl -sI http://tower:18080/<prefix>/` → `404` (pas de liste) ;
  - `http://tower:16080/vnc.html` s'ouvre et **demande un mot de passe** ;
  - `ls -ln /tmp/is-smoke` → fichiers en `99 100`.
  Ne **pas** se connecter à Instagram avec ce conteneur. Puis
  `docker rm -f is-smoke && rm -rf /tmp/is-smoke`.
- [ ] **Step 3 : commit** `feat: self-contained image with tini, Caddy and PUID/PGID`

---

### Task 7 : template Unraid, README, publication

**Files:**
- Create: `unraid/instascrap.xml`, `README.md`, `.github/workflows/release.yml`

**Interfaces:**
- Consumes : noms et défauts des variables (Global Constraints), comportement des
  Tasks 3 à 6.

Contenu requis :
- **Template** (format `dockerMan` v2) : `Repository` `ghcr.io/imthai/instascrap:latest`,
  `Network` `bridge`, pas de `WebUI`, `Support`/`Project` vers
  `https://github.com/imTHAI/instascrap`, `Overview` en anglais (2–3 phrases, dont
  l'avertissement compte dédié). `Config` : `ACCOUNTS`, `PUBLIC_URL`,
  `VNC_PASSWORD` (`Mask="true"`), `SCHEDULE` en `Display="always"`,
  `Required="true"` pour les trois premiers ; `/data` (défaut
  `/mnt/user/appdata/instascrap`), ports 8080 et 6080 en `always` ; tous les autres
  en `Display="advanced"`. Les descriptions disent : `VNC_PASSWORD` « only the first
  8 characters count », port 6080 « LAN only, never behind a reverse proxy ».
- **README** : sections du §8 de la spec, dans cet ordre, avec la règle de
  fraîcheur chiffrée et l'exemple 3 × 7 = 21 visites/jour. Ajouter une section
  « Advanced: serve the files with your own web server » : monter
  `/data/public` (lecture seule) dans son serveur, et reproduire obligatoirement
  pas de listing, `X-Robots-Tag: noindex, nofollow`, 404 hors du préfixe ;
  `reverse_proxy` vers 8080 reste la méthode recommandée. Ajouter une section
  « Manual commands » (`docker exec instascrap python3 -m instascrap --republish`,
  `--capture ACCOUNT`).
- **Workflow** : déclenché sur `push` de tags `v*` ; `permissions: packages: write,
  contents: read` ; `docker/setup-buildx-action`, `docker/login-action` (ghcr,
  `GITHUB_TOKEN`), `docker/metadata-action` (tags semver + `latest`),
  `docker/build-push-action` avec `platforms: linux/amd64`.

- [ ] **Step 1 :** valider le XML : `python3 -c "import xml.etree.ElementTree as E; E.parse('unraid/instascrap.xml')"` → aucune sortie.
- [ ] **Step 2 :** vérifier que chaque variable des Global Constraints apparaît dans
  le template **et** dans le tableau du README :
  `for v in ACCOUNTS PUBLIC_URL VNC_PASSWORD SCHEDULE ACCOUNTS_PER_SESSION FEED_ITEMS MIN_GAP_SECONDS MAX_GAP_SECONDS JITTER_MINUTES FEED_SECRET DISCORD_WEBHOOK PUID PGID; do grep -q "$v" unraid/instascrap.xml && grep -q "$v" README.md || echo "MISSING $v"; done` → aucune sortie.
- [ ] **Step 3 : commit** `docs: Unraid template, README and release workflow`
- [ ] **Step 4 (avec l'accord de l'utilisateur) :** passer `pyproject.toml` en
  `0.2.0`, taguer `v0.2.0`, pousser ; vérifier que le workflow publie
  `ghcr.io/imthai/instascrap:0.2.0` et `:latest`. Le passage du paquet ghcr en
  public se fait par l'utilisateur dans l'interface GitHub.

---

### Task 8 : migration de tower et retrait de l'ancien déploiement

Chaque action sur tower est annoncée à l'utilisateur avant exécution
(production).

- [ ] **Step 1 : relevés.** `docker inspect Caddy` (réseau du conteneur Caddy),
  `ss -ltn | grep -E ':(8080|6080)\b'` (ports hôtes libres ?), le bloc
  `feeds.example.com` du Caddyfile de tower, la liste `accounts` et les
  réglages de `config.toml`, l'URL du webhook Discord du homelab
  (`/mnt/user/homedir/scripts`). Si 8080 ou 6080 est pris, choisir d'autres
  ports **hôtes** ; les ports conteneur ne changent pas.
- [ ] **Step 2 : désactiver** l'entrée User Scripts (schedule `disabled`), vérifier
  qu'aucun conteneur `instascrap` ne tourne.
- [ ] **Step 3 : créer le conteneur** depuis le template (copié dans
  `/boot/config/plugins/dockerMan/templates-user/my-instascrap.xml`) avec :
  `ACCOUNTS` = liste de `config.toml`, `PUBLIC_URL=https://feeds.example.com`,
  `FEED_SECRET=3f9a1c0b7d2e4a68`, `/data` = `/mnt/user/appdata/instascrap`,
  réglages avancés recopiés, `DISCORD_WEBHOOK`, un `VNC_PASSWORD` choisi par
  l'utilisateur. Image : `ghcr.io/imthai/instascrap:latest` (ou `instascrap:next`
  local si la Task 7 n'est pas encore publiée).
- [ ] **Step 4 : vérifier le démarrage.** Logs : chown effectué, `Feeds index:
  https://feeds.example.com/3f9a1c0b7d2e4a68/index.html`, **aucune** alerte
  `Instagram login needed` (le profil existant est repris).
- [ ] **Step 5 : basculer Caddy.** Dans le bloc `feeds.example.com`, remplacer
  `root`/`file_server` par `reverse_proxy <hôte-ou-conteneur>:<port>` en gardant
  les en-têtes existants ; recharger Caddy.
- [ ] **Step 6 : vérifier les URL.** `curl -sI` → `200` sur
  `https://feeds.example.com/3f9a1c0b7d2e4a68/camillelouofficiel.xml` et sur
  `.../media/camillelouofficiel/Dd6JDD4MYsW/0_poster.jpg` ; `Range: bytes=0-99` sur
  le `.mp4` → `206`.
- [ ] **Step 7 : premier passage réel** au créneau suivant : logs `visited=[...]`,
  pas d'alerte inattendue sur Discord.
- [ ] **Step 8 : retrait.** Supprimer `deploy/`, `docs/DEPLOYMENT.md`,
  `config.example.toml`, `src/instascrap/notify.py`, `tests/test_notify.py` ; sur
  tower, mettre de côté (pas supprimer) `config.toml`, l'ancien dossier publié
  `/mnt/user/appdata/caddy/site/instacrap` et le script User Scripts,
  jusqu'à confirmation de l'utilisateur. `uv run pytest -q` → tout passe.
- [ ] **Step 9 : commit** `chore: drop the User Scripts deployment`
