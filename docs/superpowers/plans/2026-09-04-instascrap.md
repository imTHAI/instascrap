# instascrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transformer ~20 comptes Instagram publics en flux RSS auto-hébergés avec médias réhébergés, collectés par un vrai navigateur piloté, sans faire bloquer le compte utilisé.

**Architecture:** Un conteneur Docker one-shot sur tower lance Chrome via Playwright sur un profil persistant, visite une rotation de profils, intercepte les réponses JSON que la page émet elle-même, archive posts et médias sur disque, puis régénère des fichiers RSS statiques servis par le Caddy existant derrière un tunnel Cloudflare. Les modules purs (`parse`, `store`, `feed`) sont testables sans navigateur ; `browser` est la seule frontière d'intégration.

**Tech Stack:** Python 3.12, Playwright (Chrome réel), feedgen, feedparser (tests), pytest, tomllib, Docker, Caddy, Unraid User Scripts.

**Spec:** `docs/superpowers/specs/2026-09-04-instascrap-design.md`

## Global Constraints

Valeurs reprises telles quelles de la spec. Elles s'appliquent à toutes les tâches.

- Navigateur : Playwright `channel="chrome"`, `headless=True`, `locale="fr-FR"`, `timezone_id="Europe/Paris"`.
- Rotation : 7 comptes par session, choisis comme les plus anciennement visités.
- Cadence : 3 sessions/jour, décalage aléatoire 0-30 min appliqué au lancement.
- Pauses : 25-90 s entre profils ; 2-6 s entre scrolls du fil d'accueil.
- Blocage : arrêt **immédiat** de la session, aucun retry, aucun profil suivant. Backoff 24 h au premier blocage, 72 h au deuxième consécutif.
- Signaux de blocage : HTTP 401, HTTP 429, JSON contenant `feedback_required` ou `challenge_required`, redirection vers `/accounts/login` ou `/challenge/`.
- RSS : 50 items par flux ; archive disque permanente et intégrale.
- Médias : téléchargés via le contexte du navigateur (`context.request.get`), jamais par un client HTTP tiers.
- Vidéos dans le flux : `<video controls preload="none">`.
- Préfixe public : 16 caractères hexadécimaux, généré une fois, stocké dans `config.toml`.
- Notifications : blocage, échec de session, exception non rattrapée, et absence de tout post nouveau sur 7 jours consécutifs.
- Chemins de production : données `/mnt/user/appdata/instascrap/`, publication `/mnt/user/appdata/caddy/site/instacrap/`.
- Le JSON brut d'Instagram ne franchit jamais la frontière de `parse.py`.

## File Structure

| Fichier | Responsabilité |
|:---|:---|
| `src/instascrap/config.py` | Lecture et validation de `config.toml` → objet `Config` immuable |
| `src/instascrap/models.py` | `Post` et `Media` — le seul vocabulaire partagé entre modules |
| `src/instascrap/parse.py` | JSON Instagram brut → `Post` ; détection de blocage. Pur, sans I/O |
| `src/instascrap/store.py` | État, rotation, déduplication, écriture archive et médias |
| `src/instascrap/feed.py` | Archive → XML RSS |
| `src/instascrap/browser.py` | Session Playwright, navigation, interception, téléchargement médias |
| `src/instascrap/notify.py` | Notification Unraid, no-op journalisé hors Unraid |
| `src/instascrap/main.py` | Orchestration, rythme, gestion des blocages, CLI |
| `Dockerfile` | Image de production, basée sur l'image Playwright officielle |
| `deploy/run.sh` | User Script Unraid : décalage aléatoire puis `docker run` |
| `deploy/login.sh` | Première connexion interactive via noVNC |
| `deploy/Caddyfile.snippet` | Bloc à ajouter au Caddyfile existant |
| `tests/` | Miroir de `src/`, plus `tests/fixtures/` |

---

### Task 1: Squelette du projet et configuration

**Files:**
- Create: `pyproject.toml`, `src/instascrap/__init__.py`, `src/instascrap/config.py`, `config.example.toml`, `.gitignore`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: rien
- Produces: `Config` (dataclass gelée) et `load_config(path: Path) -> Config`. Champs : `accounts: tuple[str, ...]`, `data_dir: Path`, `publish_dir: Path`, `public_base_url: str`, `public_prefix: str`, `accounts_per_session: int`, `feed_items: int`, `min_gap_seconds: int`, `max_gap_seconds: int`.

- [ ] **Step 1: Initialiser le dépôt et la structure**

```bash
cd ~/Projects/instascrap
git init
mkdir -p src/instascrap tests/fixtures deploy
touch src/instascrap/__init__.py
```

- [ ] **Step 2: Écrire `pyproject.toml`**

```toml
[project]
name = "instascrap"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["playwright>=1.49", "feedgen>=1.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0", "feedparser>=6.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/instascrap"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 3: Écrire `.gitignore`**

```
__pycache__/
*.pyc
.venv/
config.toml
data/
.pytest_cache/
```

`config.toml` est ignoré : il contient le préfixe public, qui est un secret.

- [ ] **Step 4: Écrire le test qui échoue**

```python
# tests/test_config.py
from pathlib import Path
import pytest
from instascrap.config import load_config

BASE = """
accounts = {accounts}
data_dir = "/data"
publish_dir = "/publish"
public_base_url = "https://example.test"
public_prefix = "{prefix}"
accounts_per_session = 7
feed_items = 50
min_gap_seconds = 25
max_gap_seconds = 90
"""

def write_cfg(tmp_path, accounts='["alice", "bob"]', prefix="0123456789abcdef"):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(accounts=accounts, prefix=prefix))
    return path

def test_load_config_reads_all_fields(tmp_path):
    cfg = load_config(write_cfg(tmp_path))
    assert cfg.accounts == ("alice", "bob")
    assert cfg.data_dir == Path("/data")
    assert cfg.public_prefix == "0123456789abcdef"
    assert cfg.accounts_per_session == 7

def test_load_config_rejects_bad_prefix(tmp_path):
    with pytest.raises(ValueError, match="public_prefix"):
        load_config(write_cfg(tmp_path, prefix="trop-court"))

def test_load_config_rejects_empty_accounts(tmp_path):
    with pytest.raises(ValueError, match="accounts"):
        load_config(write_cfg(tmp_path, accounts="[]"))

def test_load_config_strips_trailing_slash_from_base_url(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(accounts='["a"]', prefix="0123456789abcdef")
                    .replace("https://example.test", "https://example.test/"))
    assert load_config(path).public_base_url == "https://example.test"
```

- [ ] **Step 5: Lancer les tests, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'instascrap.config'`

- [ ] **Step 6: Implémenter `config.py`**

```python
# src/instascrap/config.py
"""Configuration loading. The public_prefix is a shared secret: it is the only
thing standing between the rehosted media and anyone who guesses the hostname."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PREFIX_RE = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class Config:
    accounts: tuple[str, ...]
    data_dir: Path
    publish_dir: Path
    public_base_url: str
    public_prefix: str
    accounts_per_session: int
    feed_items: int
    min_gap_seconds: int
    max_gap_seconds: int


def load_config(path: Path) -> Config:
    raw = tomllib.loads(Path(path).read_text())

    accounts = tuple(raw["accounts"])
    if not accounts:
        raise ValueError("accounts must not be empty")

    prefix = raw["public_prefix"]
    if not PREFIX_RE.match(prefix):
        raise ValueError("public_prefix must be 16 lowercase hex characters")

    if raw["min_gap_seconds"] > raw["max_gap_seconds"]:
        raise ValueError("min_gap_seconds must not exceed max_gap_seconds")

    return Config(
        accounts=accounts,
        data_dir=Path(raw["data_dir"]),
        publish_dir=Path(raw["publish_dir"]),
        public_base_url=raw["public_base_url"].rstrip("/"),
        public_prefix=prefix,
        accounts_per_session=int(raw["accounts_per_session"]),
        feed_items=int(raw["feed_items"]),
        min_gap_seconds=int(raw["min_gap_seconds"]),
        max_gap_seconds=int(raw["max_gap_seconds"]),
    )
```

- [ ] **Step 7: Lancer les tests, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Écrire `config.example.toml`**

```toml
# Copier en config.toml et compléter. config.toml est git-ignoré.
accounts = ["compte_un", "compte_deux"]
data_dir = "/data"
publish_dir = "/publish"
public_base_url = "https://feeds.example.com"
# 16 hex, généré une fois : python3 -c "import secrets; print(secrets.token_hex(8))"
public_prefix = "0000000000000000"
accounts_per_session = 7
feed_items = 50
min_gap_seconds = 25
max_gap_seconds = 90
```

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore config.example.toml src/ tests/ docs/
git commit -m "✨ feat: project skeleton and config loading"
```

---

### Task 2: Modèle de données

**Files:**
- Create: `src/instascrap/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: rien
- Produces: `Media(kind, url, width, height, duration, path)` et `Post(shortcode, account, timestamp_utc, caption, permalink, media, is_carousel, like_count, comment_count)`, tous deux gelés, plus `Post.to_dict()` / `Post.from_dict()`.

`Media.path` est le chemin local relatif, `None` tant que le média n'est pas téléchargé. `store` le remplit via `dataclasses.replace`.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_models.py
import dataclasses
from datetime import datetime, timezone
import pytest
from instascrap.models import Media, Post

def make_post():
    return Post(
        shortcode="ABC123",
        account="alice",
        timestamp_utc=datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc),
        caption="Bonjour à tous 🎬",
        permalink="https://www.instagram.com/p/ABC123/",
        media=(
            Media(kind="image", url="https://cdn.test/1.jpg", width=1080, height=1350,
                  duration=None, path=None),
            Media(kind="video", url="https://cdn.test/2.mp4", width=1080, height=1920,
                  duration=14.5, path="media/alice/ABC123/1.mp4"),
        ),
        is_carousel=True,
        like_count=42,
        comment_count=7,
    )

def test_post_roundtrips_through_dict():
    post = make_post()
    assert Post.from_dict(post.to_dict()) == post

def test_to_dict_serialises_timestamp_as_iso8601():
    assert make_post().to_dict()["timestamp_utc"] == "2026-09-01T12:30:00+00:00"

def test_media_is_immutable():
    media = make_post().media[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        media.path = "nope"
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_models.py -v`
Expected: FAIL — `No module named 'instascrap.models'`

- [ ] **Step 3: Implémenter `models.py`**

```python
# src/instascrap/models.py
"""The vocabulary shared by every module. Instagram's raw JSON stops at parse.py;
everything downstream speaks only these two types."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Media:
    kind: Literal["image", "video"]
    url: str
    width: int
    height: int
    duration: float | None
    # Relative to the data dir; None until store.py has written the bytes.
    path: str | None


@dataclass(frozen=True)
class Post:
    shortcode: str
    account: str
    timestamp_utc: datetime
    caption: str
    permalink: str
    media: tuple[Media, ...]
    is_carousel: bool
    like_count: int | None
    comment_count: int | None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["timestamp_utc"] = self.timestamp_utc.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        return cls(
            shortcode=data["shortcode"],
            account=data["account"],
            timestamp_utc=datetime.fromisoformat(data["timestamp_utc"]),
            caption=data["caption"],
            permalink=data["permalink"],
            media=tuple(Media(**m) for m in data["media"]),
            is_carousel=data["is_carousel"],
            like_count=data["like_count"],
            comment_count=data["comment_count"],
        )
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/models.py tests/test_models.py
git commit -m "✨ feat: Post and Media models with JSON roundtrip"
```

---

### Task 3: Extraction des posts depuis le JSON Instagram

**Files:**
- Create: `src/instascrap/parse.py`, `tests/fixtures/web_profile_info.json`, `tests/fixtures/timeline_graphql.json`, `tests/fixtures/unrelated.json`
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: `Post`, `Media` (Task 2)
- Produces: `extract_posts(payload: dict, account: str) -> list[Post]` — retourne `[]` pour tout payload non reconnu, ce qui permet à `main` de lui passer sans filtrer toutes les réponses interceptées.

> **Important pour l'exécutant :** ces fixtures sont *synthétiques*, construites d'après la forme documentée des réponses Instagram. Elles seront confrontées à des captures réelles en Task 12 Step 6, et `extract_posts` devra probablement être ajusté à ce moment-là. C'est délibéré : le compte dédié n'est pas utilisable avant la fin de son réchauffement, et attendre bloquerait tout le reste.

- [ ] **Step 1: Écrire la fixture `tests/fixtures/web_profile_info.json`**

```json
{
  "data": {
    "user": {
      "username": "alice",
      "edge_owner_to_timeline_media": {
        "edges": [
          {
            "node": {
              "shortcode": "ABC123",
              "taken_at_timestamp": 1756729800,
              "is_video": false,
              "display_url": "https://cdn.test/ABC123.jpg",
              "dimensions": {"width": 1080, "height": 1350},
              "edge_media_to_caption": {"edges": [{"node": {"text": "Première photo"}}]},
              "edge_liked_by": {"count": 42},
              "edge_media_to_comment": {"count": 7}
            }
          },
          {
            "node": {
              "shortcode": "VID999",
              "taken_at_timestamp": 1756643400,
              "is_video": true,
              "display_url": "https://cdn.test/VID999_poster.jpg",
              "video_url": "https://cdn.test/VID999.mp4",
              "video_duration": 14.5,
              "dimensions": {"width": 1080, "height": 1920},
              "edge_media_to_caption": {"edges": []},
              "edge_liked_by": {"count": 5},
              "edge_media_to_comment": {"count": 0}
            }
          },
          {
            "node": {
              "shortcode": "CAR777",
              "taken_at_timestamp": 1756557000,
              "is_video": false,
              "display_url": "https://cdn.test/CAR777_cover.jpg",
              "dimensions": {"width": 1080, "height": 1080},
              "edge_media_to_caption": {"edges": [{"node": {"text": "Carrousel"}}]},
              "edge_liked_by": {"count": 11},
              "edge_media_to_comment": {"count": 2},
              "edge_sidecar_to_children": {
                "edges": [
                  {"node": {"is_video": false,
                            "display_url": "https://cdn.test/CAR777_1.jpg",
                            "dimensions": {"width": 1080, "height": 1080}}},
                  {"node": {"is_video": true,
                            "display_url": "https://cdn.test/CAR777_2p.jpg",
                            "video_url": "https://cdn.test/CAR777_2.mp4",
                            "video_duration": 8.0,
                            "dimensions": {"width": 1080, "height": 1080}}}
                ]
              }
            }
          }
        ]
      }
    }
  }
}
```

- [ ] **Step 2: Écrire la fixture `tests/fixtures/timeline_graphql.json`**

Forme paginée, rencontrée au scroll : même nœud, enveloppe différente.

```json
{
  "data": {
    "xdt_api__v1__feed__user_timeline_graphql_connection": {
      "edges": [
        {
          "node": {
            "shortcode": "PAG001",
            "taken_at_timestamp": 1756470600,
            "is_video": false,
            "display_url": "https://cdn.test/PAG001.jpg",
            "dimensions": {"width": 640, "height": 640},
            "edge_media_to_caption": {"edges": [{"node": {"text": "Ancien post"}}]},
            "edge_liked_by": {"count": 3},
            "edge_media_to_comment": {"count": 1}
          }
        }
      ]
    }
  }
}
```

- [ ] **Step 3: Écrire la fixture `tests/fixtures/unrelated.json`**

```json
{"status": "ok", "data": {"viewer": {"user": {"username": "moi"}}}}
```

- [ ] **Step 4: Écrire le test qui échoue**

```python
# tests/test_parse.py
import json
from datetime import timezone
from pathlib import Path
from instascrap.parse import extract_posts

FIXTURES = Path(__file__).parent / "fixtures"

def load(name):
    return json.loads((FIXTURES / name).read_text())

def test_extracts_three_posts_from_web_profile_info():
    posts = extract_posts(load("web_profile_info.json"), "alice")
    assert [p.shortcode for p in posts] == ["ABC123", "VID999", "CAR777"]

def test_image_post_fields():
    post = extract_posts(load("web_profile_info.json"), "alice")[0]
    assert post.account == "alice"
    assert post.caption == "Première photo"
    assert post.permalink == "https://www.instagram.com/p/ABC123/"
    assert post.timestamp_utc.tzinfo == timezone.utc
    assert post.like_count == 42 and post.comment_count == 7
    assert post.is_carousel is False
    assert len(post.media) == 1
    assert post.media[0].kind == "image"
    assert post.media[0].url == "https://cdn.test/ABC123.jpg"
    assert post.media[0].path is None

def test_video_post_uses_video_url_and_duration():
    post = extract_posts(load("web_profile_info.json"), "alice")[1]
    assert post.caption == ""
    assert post.media[0].kind == "video"
    assert post.media[0].url == "https://cdn.test/VID999.mp4"
    assert post.media[0].duration == 14.5

def test_carousel_expands_children_not_cover():
    post = extract_posts(load("web_profile_info.json"), "alice")[2]
    assert post.is_carousel is True
    assert [m.kind for m in post.media] == ["image", "video"]
    assert post.media[1].url == "https://cdn.test/CAR777_2.mp4"

def test_extracts_from_paginated_graphql_shape():
    assert [p.shortcode for p in extract_posts(load("timeline_graphql.json"), "alice")] == ["PAG001"]

def test_unrelated_payload_yields_nothing():
    assert extract_posts(load("unrelated.json"), "alice") == []

def test_garbage_payload_does_not_raise():
    assert extract_posts({"totally": ["unexpected"]}, "alice") == []
```

- [ ] **Step 5: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_parse.py -v`
Expected: FAIL — `No module named 'instascrap.parse'`

- [ ] **Step 6: Implémenter `parse.py`**

```python
# src/instascrap/parse.py
"""Instagram JSON -> Post. Pure and I/O-free by design: this is the module that
breaks when Instagram reshapes its responses, and fixtures make that a five-minute
fix rather than a scraping session."""

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
```

- [ ] **Step 7: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_parse.py -v`
Expected: PASS (7 tests)

- [ ] **Step 8: Commit**

```bash
git add src/instascrap/parse.py tests/test_parse.py tests/fixtures/
git commit -m "✨ feat: extract posts from Instagram JSON payloads"
```

---

### Task 4: Détection de blocage

**Files:**
- Modify: `src/instascrap/parse.py`
- Test: `tests/test_block_detection.py`

**Interfaces:**
- Consumes: rien
- Produces: `detect_block(status: int, payload: dict | None, url: str) -> str | None` — une raison lisible, ou `None` si rien d'anormal.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_block_detection.py
from instascrap.parse import detect_block

OK_URL = "https://www.instagram.com/api/v1/users/web_profile_info/?username=alice"

def test_normal_response_is_not_a_block():
    assert detect_block(200, {"status": "ok"}, OK_URL) is None

def test_401_is_a_block():
    assert "401" in detect_block(401, None, OK_URL)

def test_429_is_a_block():
    assert "429" in detect_block(429, None, OK_URL)

def test_feedback_required_is_a_block():
    assert "feedback_required" in detect_block(400, {"message": "feedback_required"}, OK_URL)

def test_challenge_required_is_a_block():
    assert "challenge_required" in detect_block(400, {"message": "challenge_required"}, OK_URL)

def test_redirect_to_login_is_a_block():
    assert detect_block(200, None, "https://www.instagram.com/accounts/login/?next=/alice/")

def test_challenge_url_is_a_block():
    assert detect_block(200, None, "https://www.instagram.com/challenge/AbCd/")

def test_nested_feedback_required_is_found():
    assert detect_block(200, {"data": {"error": {"message": "feedback_required"}}}, OK_URL)
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_block_detection.py -v`
Expected: FAIL — `cannot import name 'detect_block'`

- [ ] **Step 3: Ajouter `detect_block` à la fin de `parse.py`**

L'import `json` va en tête de fichier avec les autres imports.

```python
# src/instascrap/parse.py — ajout
import json

_BLOCK_MARKERS = ("feedback_required", "challenge_required")
_BLOCK_PATHS = ("/accounts/login", "/challenge/")


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
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_block_detection.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/parse.py tests/test_block_detection.py
git commit -m "✨ feat: detect Instagram block signals"
```

---

### Task 5: État, rotation et backoff

**Files:**
- Create: `src/instascrap/store.py`
- Test: `tests/test_store_state.py`

**Interfaces:**
- Consumes: rien
- Produces: `Store(data_dir: Path)` avec `visit_order(accounts, n) -> list[str]`, `mark_visited(account, now)`, `blocked_until() -> datetime | None`, `record_block(reason, now) -> datetime`, `clear_block()`, `last_new_post_at() -> datetime | None`, `mark_new_post(now)`.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_store_state.py
from datetime import datetime, timedelta, timezone
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

def test_never_visited_accounts_come_first(tmp_path):
    store = Store(tmp_path)
    store.mark_visited("alice", NOW)
    order = store.visit_order(["alice", "bob", "carol"], 3)
    assert order[-1] == "alice"
    assert set(order[:2]) == {"bob", "carol"}

def test_visit_order_respects_limit(tmp_path):
    assert len(Store(tmp_path).visit_order(["a", "b", "c", "d"], 2)) == 2

def test_oldest_visit_wins(tmp_path):
    store = Store(tmp_path)
    store.mark_visited("alice", NOW - timedelta(days=2))
    store.mark_visited("bob", NOW - timedelta(hours=1))
    assert store.visit_order(["alice", "bob"], 1) == ["alice"]

def test_state_survives_reinstantiation(tmp_path):
    Store(tmp_path).mark_visited("alice", NOW)
    assert Store(tmp_path).visit_order(["alice", "bob"], 1) == ["bob"]

def test_no_block_by_default(tmp_path):
    assert Store(tmp_path).blocked_until() is None

def test_first_block_backs_off_24h(tmp_path):
    assert Store(tmp_path).record_block("HTTP 429", NOW) == NOW + timedelta(hours=24)

def test_second_consecutive_block_backs_off_72h(tmp_path):
    store = Store(tmp_path)
    store.record_block("HTTP 429", NOW)
    assert store.record_block("HTTP 429", NOW) == NOW + timedelta(hours=72)

def test_clear_block_resets_the_streak(tmp_path):
    store = Store(tmp_path)
    store.record_block("HTTP 429", NOW)
    store.clear_block()
    assert store.blocked_until() is None
    assert store.record_block("HTTP 429", NOW) == NOW + timedelta(hours=24)

def test_last_new_post_tracks_most_recent(tmp_path):
    store = Store(tmp_path)
    assert store.last_new_post_at() is None
    store.mark_new_post(NOW)
    assert store.last_new_post_at() == NOW
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_store_state.py -v`
Expected: FAIL — `No module named 'instascrap.store'`

- [ ] **Step 3: Implémenter la partie état de `store.py`**

```python
# src/instascrap/store.py
"""On-disk archive and session state.

The archive is the source of truth; feeds are a disposable projection of it.
State writes go through a temp file and a rename, because a session can be killed
mid-write when the container is stopped."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

FIRST_BACKOFF = timedelta(hours=24)
REPEAT_BACKOFF = timedelta(hours=72)


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"

    def _read(self) -> dict:
        if not self.state_path.exists():
            return {"visited": {}, "block": None, "last_new_post": None}
        return json.loads(self.state_path.read_text())

    def _write(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, self.state_path)

    # --- rotation -------------------------------------------------------

    def visit_order(self, accounts, n: int) -> list[str]:
        visited = self._read()["visited"]
        # Never-visited sorts first: the empty string precedes any ISO timestamp.
        return sorted(accounts, key=lambda a: visited.get(a, ""))[:n]

    def mark_visited(self, account: str, now: datetime) -> None:
        state = self._read()
        state["visited"][account] = now.isoformat()
        self._write(state)

    # --- blocking -------------------------------------------------------

    def blocked_until(self) -> datetime | None:
        block = self._read()["block"]
        return datetime.fromisoformat(block["until"]) if block else None

    def record_block(self, reason: str, now: datetime) -> datetime:
        state = self._read()
        backoff = REPEAT_BACKOFF if state["block"] else FIRST_BACKOFF
        until = now + backoff
        state["block"] = {"reason": reason, "at": now.isoformat(), "until": until.isoformat()}
        self._write(state)
        return until

    def clear_block(self) -> None:
        state = self._read()
        state["block"] = None
        self._write(state)

    # --- freshness watchdog ---------------------------------------------

    def last_new_post_at(self) -> datetime | None:
        value = self._read()["last_new_post"]
        return datetime.fromisoformat(value) if value else None

    def mark_new_post(self, now: datetime) -> None:
        state = self._read()
        state["last_new_post"] = now.isoformat()
        self._write(state)
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_store_state.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/store.py tests/test_store_state.py
git commit -m "✨ feat: session state, rotation and block backoff"
```

---

### Task 6: Archive des posts et des médias

**Files:**
- Modify: `src/instascrap/store.py`
- Test: `tests/test_store_archive.py`

**Interfaces:**
- Consumes: `Post`, `Media` (Task 2), `Store` (Task 5)
- Produces: `Store.known_shortcodes(account) -> set[str]`, `Store.save_post(post, media_bytes: dict[str, bytes]) -> Post`, `Store.load_posts(account, limit=None) -> list[Post]`, `Store.archive_raw(account, payload, now) -> Path`.

`save_post` reçoit les octets déjà téléchargés, indexés par URL : le téléchargement appartient à `browser.py`, l'écriture à `store.py`. Il retourne le `Post` avec les `Media.path` remplis.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_store_archive.py
import gzip
import json
from datetime import datetime, timedelta, timezone
from instascrap.models import Media, Post
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)
BYTES = {"https://cdn.test/1.jpg": b"jpegdata", "https://cdn.test/2.mp4": b"mp4data"}

def make_post(shortcode="ABC123", ts=NOW):
    return Post(
        shortcode=shortcode, account="alice", timestamp_utc=ts,
        caption="Salut", permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=(
            Media("image", "https://cdn.test/1.jpg", 1080, 1080, None, None),
            Media("video", "https://cdn.test/2.mp4", 1080, 1920, 3.0, None),
        ),
        is_carousel=True, like_count=1, comment_count=0,
    )

def test_save_post_writes_media_with_correct_extensions(tmp_path):
    saved = Store(tmp_path).save_post(make_post(), BYTES)
    assert saved.media[0].path == "media/alice/ABC123/0.jpg"
    assert saved.media[1].path == "media/alice/ABC123/1.mp4"
    assert (tmp_path / saved.media[0].path).read_bytes() == b"jpegdata"

def test_save_post_writes_json_metadata(tmp_path):
    Store(tmp_path).save_post(make_post(), BYTES)
    data = json.loads((tmp_path / "posts/alice/ABC123.json").read_text())
    assert data["shortcode"] == "ABC123"
    assert data["media"][0]["path"] == "media/alice/ABC123/0.jpg"

def test_known_shortcodes_reflects_archive(tmp_path):
    store = Store(tmp_path)
    assert store.known_shortcodes("alice") == set()
    store.save_post(make_post("AAA"), BYTES)
    store.save_post(make_post("BBB"), BYTES)
    assert store.known_shortcodes("alice") == {"AAA", "BBB"}

def test_saving_twice_is_idempotent(tmp_path):
    store = Store(tmp_path)
    store.save_post(make_post(), BYTES)
    first = (tmp_path / "media/alice/ABC123/0.jpg").stat().st_mtime_ns
    store.save_post(make_post(), BYTES)
    assert (tmp_path / "media/alice/ABC123/0.jpg").stat().st_mtime_ns == first
    assert store.known_shortcodes("alice") == {"ABC123"}

def test_load_posts_returns_newest_first(tmp_path):
    store = Store(tmp_path)
    store.save_post(make_post("OLD", NOW - timedelta(days=3)), BYTES)
    store.save_post(make_post("NEW", NOW), BYTES)
    assert [p.shortcode for p in store.load_posts("alice")] == ["NEW", "OLD"]

def test_load_posts_honours_limit(tmp_path):
    store = Store(tmp_path)
    for index, code in enumerate(("A", "B", "C")):
        store.save_post(make_post(code, NOW - timedelta(days=index)), BYTES)
    assert len(store.load_posts("alice", limit=2)) == 2

def test_load_posts_on_unknown_account_is_empty(tmp_path):
    assert Store(tmp_path).load_posts("nobody") == []

def test_archive_raw_writes_gzipped_json(tmp_path):
    path = Store(tmp_path).archive_raw("alice", {"hello": "world"}, NOW)
    assert path.suffix == ".gz"
    assert json.loads(gzip.decompress(path.read_bytes())) == {"hello": "world"}
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_store_archive.py -v`
Expected: FAIL — `'Store' object has no attribute 'save_post'`

- [ ] **Step 3: Compléter les imports en tête de `store.py`**

```python
# src/instascrap/store.py — en-tête, après les imports existants
import dataclasses
import gzip
from urllib.parse import urlparse

from .models import Post

_EXT = {"image": ".jpg", "video": ".mp4"}
```

- [ ] **Step 4: Ajouter ces méthodes dans la classe `Store`**

À la suite des méthodes existantes, au même niveau d'indentation.

```python
    # --- archive --------------------------------------------------------

    def _posts_dir(self, account: str) -> Path:
        return self.data_dir / "posts" / account

    def known_shortcodes(self, account: str) -> set[str]:
        directory = self._posts_dir(account)
        return {p.stem for p in directory.glob("*.json")} if directory.exists() else set()

    def save_post(self, post: Post, media_bytes: dict[str, bytes]) -> Post:
        media_dir = self.data_dir / "media" / post.account / post.shortcode
        media_dir.mkdir(parents=True, exist_ok=True)

        stored = []
        for index, media in enumerate(post.media):
            ext = _EXT.get(media.kind) or Path(urlparse(media.url).path).suffix or ".bin"
            rel = f"media/{post.account}/{post.shortcode}/{index}{ext}"
            target = self.data_dir / rel
            # An existing file is never rewritten: replaying a session must leave
            # mtimes and hard links untouched.
            if not target.exists():
                target.write_bytes(media_bytes[media.url])
            stored.append(dataclasses.replace(media, path=rel))

        saved = dataclasses.replace(post, media=tuple(stored))

        self._posts_dir(post.account).mkdir(parents=True, exist_ok=True)
        json_path = self._posts_dir(post.account) / f"{post.shortcode}.json"
        if not json_path.exists():
            json_path.write_text(json.dumps(saved.to_dict(), indent=2, ensure_ascii=False))
        return saved

    def load_posts(self, account: str, limit: int | None = None) -> list[Post]:
        directory = self._posts_dir(account)
        if not directory.exists():
            return []
        posts = [Post.from_dict(json.loads(p.read_text())) for p in directory.glob("*.json")]
        posts.sort(key=lambda p: p.timestamp_utc, reverse=True)
        return posts[:limit] if limit else posts

    def archive_raw(self, account: str, payload: dict, now: datetime) -> Path:
        directory = self.data_dir / "raw" / account
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{now.strftime('%Y%m%dT%H%M%S%f')}.json.gz"
        path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        return path
```

- [ ] **Step 5: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_store_archive.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add src/instascrap/store.py tests/test_store_archive.py
git commit -m "✨ feat: archive posts, media and raw payloads"
```

---

### Task 7: Génération des flux RSS

**Files:**
- Create: `src/instascrap/feed.py`
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `Post` (Task 2), `Store` (Tasks 5-6)
- Produces: `build_feed(account: str, posts: Sequence[Post], cfg: Config) -> str` et `publish(store: Store, cfg: Config) -> list[Path]`.

`publish` écrit les XML dans `cfg.publish_dir / cfg.public_prefix /` et y crée les liens durs des médias.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_feed.py
from datetime import datetime, timedelta, timezone
import feedparser
from instascrap.config import Config
from instascrap.feed import build_feed, publish
from instascrap.models import Media, Post
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

def cfg_for(tmp_path, feed_items=2):
    return Config(
        accounts=("alice",), data_dir=tmp_path / "data", publish_dir=tmp_path / "pub",
        public_base_url="https://example.test", public_prefix="0123456789abcdef",
        accounts_per_session=7, feed_items=feed_items, min_gap_seconds=25, max_gap_seconds=90,
    )

def post(shortcode, kind="image", ts=NOW, caption="Légende & co"):
    ext = ".jpg" if kind == "image" else ".mp4"
    return Post(
        shortcode=shortcode, account="alice", timestamp_utc=ts, caption=caption,
        permalink=f"https://www.instagram.com/p/{shortcode}/",
        media=(Media(kind, f"https://cdn.test/{shortcode}{ext}", 1080, 1080,
                     None if kind == "image" else 5.0,
                     f"media/alice/{shortcode}/0{ext}"),),
        is_carousel=False, like_count=3, comment_count=1,
    )

def test_feed_is_valid_rss_with_one_item_per_post(tmp_path):
    parsed = feedparser.parse(build_feed("alice", [post("AAA"), post("BBB")], cfg_for(tmp_path)))
    assert parsed.bozo is False
    assert len(parsed.entries) == 2
    assert parsed.feed.title == "Instagram — alice"

def test_item_links_to_original_post(tmp_path):
    parsed = feedparser.parse(build_feed("alice", [post("AAA")], cfg_for(tmp_path)))
    assert parsed.entries[0].link == "https://www.instagram.com/p/AAA/"

def test_image_is_embedded_with_public_url(tmp_path):
    xml = build_feed("alice", [post("AAA")], cfg_for(tmp_path))
    assert "https://example.test/0123456789abcdef/media/alice/AAA/0.jpg" in xml
    assert "&lt;img" in xml or "<img" in xml

def test_video_uses_video_tag_with_preload_none(tmp_path):
    xml = build_feed("alice", [post("VID", kind="video")], cfg_for(tmp_path))
    assert "video" in xml and "preload=" in xml and "controls" in xml

def test_caption_html_is_escaped(tmp_path):
    parsed = feedparser.parse(
        build_feed("alice", [post("AAA", caption="<script>x</script>")], cfg_for(tmp_path)))
    assert "<script>x</script>" not in parsed.entries[0].description

def test_publish_writes_one_file_per_account_plus_all(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    names = {p.name for p in publish(store, cfg)}
    assert "alice.xml" in names and "all.xml" in names

def test_publish_hardlinks_media_into_public_dir(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    public = cfg.publish_dir / cfg.public_prefix / "media/alice/AAA/0.jpg"
    source = cfg.data_dir / "media/alice/AAA/0.jpg"
    assert public.read_bytes() == b"jpeg"
    assert public.stat().st_ino == source.stat().st_ino

def test_publish_is_idempotent(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.save_post(post("AAA"), {"https://cdn.test/AAA.jpg": b"jpeg"})
    publish(store, cfg)
    publish(store, cfg)  # must not raise on existing hard links

def test_feed_respects_item_limit(tmp_path):
    cfg = cfg_for(tmp_path, feed_items=2)
    store = Store(cfg.data_dir)
    for index, code in enumerate(("A", "B", "C")):
        store.save_post(post(code, ts=NOW - timedelta(days=index)),
                        {f"https://cdn.test/{code}.jpg": b"jpeg"})
    publish(store, cfg)
    parsed = feedparser.parse((cfg.publish_dir / cfg.public_prefix / "alice.xml").read_text())
    assert len(parsed.entries) == 2
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest --with feedparser --with feedgen pytest tests/test_feed.py -v`
Expected: FAIL — `No module named 'instascrap.feed'`

- [ ] **Step 3: Implémenter `feed.py`**

```python
# src/instascrap/feed.py
"""Archive -> RSS. Feeds are regenerated wholesale every session; nothing here is
incremental, so a partial write can never leave a half-correct feed behind."""

import os
from html import escape
from pathlib import Path
from typing import Sequence

from feedgen.feed import FeedGenerator

from .config import Config
from .models import Post
from .store import Store


def _public_url(cfg: Config, rel_path: str) -> str:
    return f"{cfg.public_base_url}/{cfg.public_prefix}/{rel_path}"


def _describe(post: Post, cfg: Config) -> str:
    parts = []
    for media in post.media:
        if media.path is None:
            continue
        url = escape(_public_url(cfg, media.path), quote=True)
        if media.kind == "video":
            # preload="none" matters: News Explorer would otherwise pull every
            # video in the list view.
            parts.append(f'<video controls preload="none" width="{media.width}" src="{url}"></video>')
        else:
            parts.append(f'<img src="{url}" width="{media.width}" height="{media.height}">')
    if post.caption:
        parts.append(f"<p>{escape(post.caption)}</p>")
    return "".join(parts)


def build_feed(account: str, posts: Sequence[Post], cfg: Config) -> str:
    generator = FeedGenerator()
    generator.id(f"{cfg.public_base_url}/{cfg.public_prefix}/{account}.xml")
    generator.title(f"Instagram — {account}")
    generator.link(href=f"https://www.instagram.com/{account}/", rel="alternate")
    generator.description(f"Posts de @{account}, réhébergés")
    generator.language("fr")

    # feedgen emits items in reverse insertion order, so insert oldest first.
    for post in sorted(posts, key=lambda p: p.timestamp_utc):
        entry = generator.add_entry()
        entry.id(post.permalink)
        entry.guid(post.permalink, permalink=True)
        entry.title(post.caption.splitlines()[0][:120] if post.caption
                    else f"@{post.account} — {post.timestamp_utc:%d/%m/%Y}")
        entry.link(href=post.permalink)
        entry.pubDate(post.timestamp_utc)
        entry.description(_describe(post, cfg))
        primary = post.media[0] if post.media else None
        if primary is not None and primary.path:
            entry.enclosure(_public_url(cfg, primary.path), 0,
                            "video/mp4" if primary.kind == "video" else "image/jpeg")

    return generator.rss_str(pretty=True).decode()


def _hardlink(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.link(source, target)
    except OSError:
        # Falls back to a copy when data and publish sit on different filesystems.
        target.write_bytes(source.read_bytes())


def publish(store: Store, cfg: Config) -> list[Path]:
    out_dir = cfg.publish_dir / cfg.public_prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    everything: list[Post] = []

    for account in cfg.accounts:
        posts = store.load_posts(account, limit=cfg.feed_items)
        everything.extend(posts)

        path = out_dir / f"{account}.xml"
        path.write_text(build_feed(account, posts, cfg))
        written.append(path)

        for post in posts:
            for media in post.media:
                if media.path:
                    _hardlink(store.data_dir / media.path, out_dir / media.path)

    everything.sort(key=lambda p: p.timestamp_utc, reverse=True)
    all_path = out_dir / "all.xml"
    all_path.write_text(build_feed("tous les comptes", everything[:cfg.feed_items], cfg))
    written.append(all_path)
    return written
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest --with feedparser --with feedgen pytest tests/test_feed.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/feed.py tests/test_feed.py
git commit -m "✨ feat: generate RSS feeds and publish media hard links"
```

---

### Task 8: Notifications

**Files:**
- Create: `src/instascrap/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Consumes: rien
- Produces: `notify(summary: str, description: str, level: str = "warning") -> bool` — `True` si la notification Unraid a été émise, `False` si le script est absent (dev sur Mac), auquel cas elle est journalisée.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_notify.py
import logging
from instascrap import notify as notify_mod

def test_returns_false_and_logs_when_unraid_script_absent(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(notify_mod, "NOTIFY_SCRIPT", tmp_path / "does-not-exist")
    assert notify_mod.notify("Titre", "Détail") is False
    assert "Titre" in caplog.text

def test_invokes_unraid_script_with_expected_arguments(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(notify_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    script = tmp_path / "notify"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(notify_mod, "NOTIFY_SCRIPT", script)

    assert notify_mod.notify("Bloqué", "HTTP 429", level="alert") is True
    assert "Bloqué" in calls[0]
    assert "alert" in calls[0]
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_notify.py -v`
Expected: FAIL — `No module named 'instascrap.notify'`

- [ ] **Step 3: Implémenter `notify.py`**

```python
# src/instascrap/notify.py
"""Unraid notifications. Silent failure is the enemy: a broken parser would
otherwise show up as feeds that quietly stop growing, discovered weeks later."""

import logging
import subprocess
from pathlib import Path

NOTIFY_SCRIPT = Path("/usr/local/emhttp/webGui/scripts/notify")

log = logging.getLogger(__name__)


def notify(summary: str, description: str, level: str = "warning") -> bool:
    if not NOTIFY_SCRIPT.exists():
        log.warning("notification (no Unraid host): %s — %s", summary, description)
        return False

    subprocess.run(
        [str(NOTIFY_SCRIPT), "-e", "instascrap", "-s", summary,
         "-d", description, "-i", level],
        check=False,
    )
    return True
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest pytest tests/test_notify.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/notify.py tests/test_notify.py
git commit -m "✨ feat: Unraid notifications with dev-host fallback"
```

---

### Task 9: Session navigateur

**Files:**
- Create: `src/instascrap/browser.py`
- Test: `tests/test_browser_contract.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `detect_block` (Task 4)
- Produces: `BlockedError`, `collect_payload(status, url, payload) -> dict | None`, et `Session` utilisable en gestionnaire de contexte avec `warm_up()`, `visit_profile(account) -> list[dict]`, `fetch_media(url) -> bytes`, `gap()`.

> **Frontière d'intégration.** `Session` n'est pas testé unitairement — lancer un vrai Chrome en test unitaire n'apporterait rien. Les tests portent sur `collect_payload`, la décision pure qui gouverne l'interception. La validation réelle passe par `--capture` en Task 12.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_browser_contract.py
import pytest
from instascrap.browser import BlockedError, collect_payload

API = "https://www.instagram.com/api/v1/users/web_profile_info/?username=alice"

def test_collect_payload_raises_on_429():
    with pytest.raises(BlockedError, match="429"):
        collect_payload(429, API, None)

def test_collect_payload_returns_api_json():
    payload = {"data": {"user": {}}}
    assert collect_payload(200, API, payload) == payload

def test_collect_payload_ignores_non_api_urls():
    assert collect_payload(200, "https://www.instagram.com/static/app.js", {"a": 1}) is None

def test_collect_payload_ignores_api_url_without_json():
    assert collect_payload(200, API, None) is None

def test_collect_payload_detects_login_redirect():
    with pytest.raises(BlockedError, match="accounts/login"):
        collect_payload(200, "https://www.instagram.com/accounts/login/", None)

def test_collect_payload_detects_feedback_required():
    with pytest.raises(BlockedError, match="feedback_required"):
        collect_payload(400, API, {"message": "feedback_required"})
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest pytest tests/test_browser_contract.py -v`
Expected: FAIL — `No module named 'instascrap.browser'`

- [ ] **Step 3: Implémenter `browser.py`**

```python
# src/instascrap/browser.py
"""The only module that talks to a browser.

Everything Instagram-facing goes through a real Chrome on a persistent profile:
the TLS and HTTP/2 fingerprints, the JS-generated headers and the rotating
x-ig-www-claim all come for free that way, which is exactly what a Python HTTP
client cannot reproduce."""

import logging
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .config import Config
from .parse import detect_block

log = logging.getLogger(__name__)

API_MARKERS = ("/api/v1/", "/graphql/query")


class BlockedError(RuntimeError):
    """Instagram pushed back. Never retried — the session ends here."""


def collect_payload(status: int, url: str, payload: dict | None) -> dict | None:
    """Decide what to do with one intercepted response. Pure, hence testable."""
    reason = detect_block(status, payload, url)
    if reason:
        raise BlockedError(reason)
    if payload is None or not any(marker in url for marker in API_MARKERS):
        return None
    return payload


class Session:
    def __init__(self, cfg: Config, profile_dir: Path, headless: bool = True):
        self.cfg = cfg
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._captured: list[dict] = []
        self._block: BlockedError | None = None

    def __enter__(self) -> "Session":
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chrome",
            headless=self.headless,
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc) -> None:
        self.context.close()
        self._playwright.stop()

    def _on_response(self, response) -> None:
        # Playwright swallows exceptions raised inside event handlers, so a block
        # is stashed here and re-raised from visit_profile instead.
        payload = None
        if "application/json" in (response.headers.get("content-type") or ""):
            try:
                payload = response.json()
            except Exception:
                payload = None
        try:
            captured = collect_payload(response.status, response.url, payload)
        except BlockedError as exc:
            self._block = exc
            return
        if captured is not None:
            self._captured.append(captured)

    def _pause(self, low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    def warm_up(self) -> None:
        """Land on the home feed and scroll a little before touching any profile.
        Costs nothing and makes the session read as a visit rather than a sweep."""
        self._block = None
        self.page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        for _ in range(random.randint(2, 3)):
            self._pause(2, 6)
            self.page.mouse.wheel(0, random.randint(600, 1200))
        self._pause(2, 6)
        if self._block:
            raise self._block

    def visit_profile(self, account: str) -> list[dict]:
        self._captured = []
        self._block = None
        self.page.goto(f"https://www.instagram.com/{account}/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(random.randint(1500, 3500))
        if self._block:
            raise self._block
        return list(self._captured)

    def fetch_media(self, url: str) -> bytes:
        # Through the browser context, so the request carries its cookies and headers.
        return self.context.request.get(url).body()

    def gap(self) -> None:
        self._pause(self.cfg.min_gap_seconds, self.cfg.max_gap_seconds)
```

- [ ] **Step 4: Lancer, vérifier le succès**

Run: `uv run --with pytest --with playwright pytest tests/test_browser_contract.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/instascrap/browser.py tests/test_browser_contract.py
git commit -m "✨ feat: Playwright session with response interception"
```

---

### Task 10: Orchestration et CLI

**Files:**
- Create: `src/instascrap/main.py`, `src/instascrap/__main__.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: tous les modules précédents
- Produces: `SessionResult(visited: list[str], new_posts: int, blocked: str | None)`, `run_session(cfg, store, session, now) -> SessionResult`, et un CLI `python -m instascrap` avec `--run`, `--login`, `--capture ACCOUNT`, `--republish`.

`run_session` reçoit la session plutôt que de la créer : c'est ce qui rend l'orchestration testable avec un faux navigateur.

- [ ] **Step 1: Écrire le test qui échoue**

```python
# tests/test_main.py
import json
from datetime import datetime, timedelta, timezone
from instascrap.browser import BlockedError
from instascrap.config import Config
from instascrap.main import run_session
from instascrap.store import Store

NOW = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

PAYLOAD = {"data": {"user": {"edge_owner_to_timeline_media": {"edges": [
    {"node": {"shortcode": "AAA", "taken_at_timestamp": 1756729800, "is_video": False,
              "display_url": "https://cdn.test/AAA.jpg",
              "dimensions": {"width": 100, "height": 100},
              "edge_media_to_caption": {"edges": []},
              "edge_liked_by": {"count": 1},
              "edge_media_to_comment": {"count": 0}}}]}}}}

def cfg_for(tmp_path, accounts=("alice", "bob")):
    return Config(accounts=accounts, data_dir=tmp_path / "data",
                  publish_dir=tmp_path / "pub", public_base_url="https://example.test",
                  public_prefix="0123456789abcdef", accounts_per_session=2,
                  feed_items=50, min_gap_seconds=0, max_gap_seconds=0)

class FakeSession:
    def __init__(self, raise_on=None):
        self.raise_on = raise_on
        self.visited = []
        self.warmed = False

    def warm_up(self):
        self.warmed = True

    def visit_profile(self, account):
        if self.raise_on == account:
            raise BlockedError("HTTP 429 on profile")
        self.visited.append(account)
        return [PAYLOAD]

    def fetch_media(self, url):
        return b"bytes"

    def gap(self):
        pass

def test_session_warms_up_before_visiting(tmp_path):
    cfg = cfg_for(tmp_path)
    session = FakeSession()
    run_session(cfg, Store(cfg.data_dir), session, NOW)
    assert session.warmed is True

def test_new_posts_are_archived(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    result = run_session(cfg, store, FakeSession(), NOW)
    assert result.new_posts == 2  # the same payload is harvested for both accounts
    assert store.known_shortcodes("alice") == {"AAA"}

def test_already_known_posts_are_not_refetched(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    result = run_session(cfg, store, FakeSession(), NOW + timedelta(hours=8))
    assert result.new_posts == 0

def test_block_stops_the_session_immediately(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    session = FakeSession(raise_on="alice")
    result = run_session(cfg, store, session, NOW)
    assert result.blocked is not None
    assert session.visited == []                       # no fallthrough to bob
    assert store.blocked_until() == NOW + timedelta(hours=24)

def test_session_refuses_to_run_while_blocked(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.record_block("HTTP 429", NOW)
    session = FakeSession()
    result = run_session(cfg, store, session, NOW + timedelta(hours=1))
    assert result.visited == []
    assert session.warmed is False

def test_expired_block_is_cleared_and_session_runs(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    store.record_block("HTTP 429", NOW)
    result = run_session(cfg, store, FakeSession(), NOW + timedelta(hours=25))
    assert result.visited != []
    assert store.blocked_until() is None

def test_successful_session_marks_accounts_visited(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    visited = json.loads((cfg.data_dir / "state.json").read_text())["visited"]
    assert set(visited) == {"alice", "bob"}

def test_raw_payloads_are_archived(tmp_path):
    cfg = cfg_for(tmp_path)
    store = Store(cfg.data_dir)
    run_session(cfg, store, FakeSession(), NOW)
    assert list((cfg.data_dir / "raw" / "alice").glob("*.json.gz"))
```

- [ ] **Step 2: Lancer, vérifier l'échec**

Run: `uv run --with pytest --with feedgen --with playwright pytest tests/test_main.py -v`
Expected: FAIL — `No module named 'instascrap.main'`

- [ ] **Step 3: Implémenter `main.py`**

```python
# src/instascrap/main.py
"""Orchestration. The rule that outranks everything else: a block ends the session
on the spot. No retry, no next account."""

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .browser import BlockedError, Session
from .config import Config, load_config
from .feed import publish
from .notify import notify
from .parse import extract_posts
from .store import Store

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(days=7)


@dataclass
class SessionResult:
    visited: list[str] = field(default_factory=list)
    new_posts: int = 0
    blocked: str | None = None


def run_session(cfg: Config, store: Store, session, now: datetime) -> SessionResult:
    result = SessionResult()

    until = store.blocked_until()
    if until and until > now:
        log.warning("blocked until %s, doing nothing", until)
        return result
    if until:
        store.clear_block()

    try:
        session.warm_up()
    except BlockedError as exc:
        result.blocked = str(exc)
        store.record_block(str(exc), now)
        notify("instascrap bloqué", f"{exc} — pause de sécurité", level="alert")
        return result

    for account in store.visit_order(cfg.accounts, cfg.accounts_per_session):
        try:
            payloads = session.visit_profile(account)
        except BlockedError as exc:
            result.blocked = str(exc)
            store.record_block(str(exc), now)
            notify("instascrap bloqué", f"{exc} — pause de sécurité", level="alert")
            return result

        known = store.known_shortcodes(account)
        for payload in payloads:
            store.archive_raw(account, payload, now)
            for post in extract_posts(payload, account):
                if post.shortcode in known:
                    continue
                media_bytes = {media.url: session.fetch_media(media.url) for media in post.media}
                store.save_post(post, media_bytes)
                known.add(post.shortcode)
                result.new_posts += 1

        store.mark_visited(account, now)
        result.visited.append(account)
        session.gap()

    if result.new_posts:
        store.mark_new_post(now)
    return result


def _check_staleness(store: Store, now: datetime) -> None:
    last = store.last_new_post_at()
    if last and now - last > STALE_AFTER:
        notify("instascrap silencieux",
               f"Aucun post nouveau depuis {(now - last).days} jours — parser cassé ?")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(prog="instascrap")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="one collection session")
    mode.add_argument("--login", action="store_true", help="headed session for manual login")
    mode.add_argument("--capture", metavar="ACCOUNT", help="visit one profile, archive raw only")
    mode.add_argument("--republish", action="store_true", help="rebuild feeds from the archive")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    store = Store(cfg.data_dir)
    profile_dir = cfg.data_dir / "profile"
    now = datetime.now(timezone.utc)

    if args.republish:
        log.info("republished %d feeds", len(publish(store, cfg)))
        return 0

    if args.login:
        with Session(cfg, profile_dir, headless=False) as session:
            session.page.goto("https://www.instagram.com/accounts/login/")
            log.info("log in manually, then press Enter here")
            input()
        return 0

    if args.capture:
        with Session(cfg, profile_dir) as session:
            for payload in session.visit_profile(args.capture):
                log.info("captured %s", store.archive_raw(args.capture, payload, now))
        return 0

    try:
        with Session(cfg, profile_dir) as session:
            result = run_session(cfg, store, session, now)
    except Exception as exc:  # noqa: BLE001 — last line of defence before cron eats it
        log.exception("session failed")
        notify("instascrap en échec", str(exc), level="alert")
        return 1

    log.info("visited=%s new_posts=%d blocked=%s",
             result.visited, result.new_posts, result.blocked)
    if not result.blocked:
        publish(store, cfg)
    _check_staleness(store, now)
    return 0
```

- [ ] **Step 4: Écrire `__main__.py`**

```python
# src/instascrap/__main__.py
import sys

from .main import main

sys.exit(main())
```

- [ ] **Step 5: Lancer toute la suite, vérifier le succès**

Run: `uv run --with pytest --with feedgen --with feedparser --with playwright pytest tests/ -v`
Expected: PASS — 56 tests

- [ ] **Step 6: Commit**

```bash
git add src/instascrap/main.py src/instascrap/__main__.py tests/test_main.py
git commit -m "✨ feat: session orchestration and CLI"
```

---

### Task 11: Image Docker et connexion interactive

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `deploy/login.sh`
- Test: manuel, décrit ci-dessous

**Interfaces:**
- Consumes: le paquet complet
- Produces: image `instascrap:latest`, montages `/data` (données) et `/publish` (racine Caddy).

- [ ] **Step 1: Écrire le `Dockerfile`**

```dockerfile
# The Playwright image ships a Chrome matching its own version; mismatching the two
# is the usual cause of "browser not found" at runtime.
FROM mcr.microsoft.com/playwright:v1.49.0-noble

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --break-system-packages .

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "instascrap"]
CMD ["--run"]
```

- [ ] **Step 2: Écrire `.dockerignore`**

```
.git/
.venv/
data/
tests/
docs/
config.toml
__pycache__/
```

- [ ] **Step 3: Construire l'image**

```bash
docker build -t instascrap:latest ~/Projects/instascrap
```
Expected: build réussi.

- [ ] **Step 4: Vérifier que le CLI répond dans le conteneur**

```bash
docker run --rm instascrap:latest --help
```
Expected: l'aide argparse s'affiche avec `--run`, `--login`, `--capture`, `--republish`.

- [ ] **Step 5: Écrire `deploy/login.sh`**

```bash
#!/bin/bash
# First login, done by hand: starts a virtual display with noVNC so the Instagram
# session is created by a real, human-driven browser. Every later run is headless
# on the profile this produces.
set -euo pipefail

DATA=/mnt/user/appdata/instascrap

docker run --rm -it \
  -v "$DATA:/data" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  -p 6080:6080 \
  --shm-size=1g \
  --entrypoint bash \
  instascrap:latest -c '
    Xvfb :99 -screen 0 1440x900x24 &
    x11vnc -display :99 -nopw -forever -shared >/dev/null 2>&1 &
    websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    sleep 2
    echo "Ouvre http://tower:6080/vnc.html, connecte-toi, puis Entrée ici."
    DISPLAY=:99 python -m instascrap --login
  '
```

- [ ] **Step 6: Rendre exécutable et commiter**

```bash
chmod +x deploy/login.sh
git add Dockerfile .dockerignore deploy/login.sh
git commit -m "🔧 chore: Docker image and interactive login helper"
```

---

### Task 12: Déploiement sur tower

**Files:**
- Create: `deploy/run.sh`, `deploy/Caddyfile.snippet`, `tests/test_parse_real.py`, `tests/fixtures/real_profile.json`
- Test: manuel et fixture réelle, décrits ci-dessous

**Interfaces:**
- Consumes: image de la Task 11
- Produces: le système en production.

- [ ] **Step 1: Écrire `deploy/run.sh`**

```bash
#!/bin/bash
# Unraid User Script. Scheduled three times a day; the random delay is what turns
# three fixed cron entries into irregular, human-looking session times.
set -euo pipefail

sleep $((RANDOM % 1800))

DATA=/mnt/user/appdata/instascrap
PUBLISH=/mnt/user/appdata/caddy/site/instacrap

docker run --rm \
  -v "$DATA:/data" \
  -v "$PUBLISH:/publish" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  --shm-size=1g \
  instascrap:latest --run
```

`--shm-size=1g` n'est pas décoratif : Chrome meurt en `SIGBUS` avec les 64 Mo de `/dev/shm` par défaut de Docker.

- [ ] **Step 2: Écrire `deploy/Caddyfile.snippet`**

```
# À ajouter au Caddyfile existant. Remplacer PREFIX par public_prefix de config.toml.
feeds.example.com {
    root * /usr/share/caddy/instacrap
    @feed path_regexp feed ^/PREFIX/([A-Za-z0-9._]+)$
    rewrite @feed /PREFIX/{re.feed.1}.xml
    file_server
    import security_headers
}
```

- [ ] **Step 3: Générer le préfixe et déposer la configuration**

```bash
python3 -c "import secrets; print(secrets.token_hex(8))"
ssh tower "mkdir -p /mnt/user/appdata/instascrap"
```

Copier `config.example.toml` vers `/mnt/user/appdata/instascrap/config.toml` sur tower, y mettre le préfixe généré, la liste réelle des comptes, `data_dir = "/data"` et `publish_dir = "/publish"`.

- [ ] **Step 4: Construire l'image sur tower**

```bash
rsync -av --exclude .git --exclude data ~/Projects/instascrap/ \
  tower:/mnt/user/appdata/instascrap-src/
ssh tower "cd /mnt/user/appdata/instascrap-src && docker build -t instascrap:latest ."
```
Expected: build réussi sur l'hôte cible.

- [ ] **Step 5: Connexion Instagram initiale**

```bash
ssh tower "bash /mnt/user/appdata/instascrap-src/deploy/login.sh"
```

Ouvrir `http://tower:6080/vnc.html`, se connecter au compte dédié, valider le 2FA, revenir au terminal, presser Entrée.
Expected: `/mnt/user/appdata/instascrap/profile/` contient un profil Chrome peuplé.

- [ ] **Step 6: Capturer des payloads réels et confronter le parser**

C'est l'étape de vérité du projet : les fixtures des Tasks 3-4 sont synthétiques.

```bash
ssh tower "docker run --rm \
  -v /mnt/user/appdata/instascrap:/data \
  -v /mnt/user/appdata/instascrap/config.toml:/app/config.toml:ro \
  --shm-size=1g instascrap:latest --capture UN_COMPTE_CIBLE"
```

Récupérer un `.json.gz` produit dans `/data/raw/UN_COMPTE_CIBLE/`, le décompresser vers `tests/fixtures/real_profile.json`, puis écrire ce test et **ajuster `parse.py` si la forme diffère** :

```python
# tests/test_parse_real.py
import json
from pathlib import Path
from instascrap.parse import extract_posts

def test_real_capture_yields_usable_posts():
    payload = json.loads((Path(__file__).parent / "fixtures/real_profile.json").read_text())
    posts = extract_posts(payload, "un_compte_cible")
    assert posts, "aucun post extrait d'une capture réelle"
    assert all(post.media for post in posts)
    assert all(post.media[0].url.startswith("http") for post in posts)
    assert all(post.timestamp_utc.year >= 2020 for post in posts)
```

Run: `uv run --with pytest pytest tests/ -v`
Expected: PASS — toute la suite, fixture réelle comprise.

- [ ] **Step 7: Première session complète**

```bash
ssh tower "bash /mnt/user/appdata/instascrap-src/deploy/run.sh"
```
Expected: journal `visited=[...] new_posts=N blocked=None`, et des XML dans `/mnt/user/appdata/caddy/site/instacrap/<préfixe>/`.

- [ ] **Step 8: Ajouter le bloc Caddy et recharger**

Insérer le contenu de `deploy/Caddyfile.snippet` (avec le vrai préfixe) dans `/mnt/user/appdata/caddy/config/Caddyfile`, puis :

```bash
ssh tower "docker exec Caddy caddy reload --config /etc/caddy/Caddyfile"
```
Expected: rechargement sans erreur de syntaxe.

- [ ] **Step 9: Étapes manuelles côté Cloudflare**

Dans le dashboard : ajouter le hostname public `feeds.example.com` pointant vers Caddy, **et régénérer le token du tunnel** — il a été affiché en clair pendant la conception.

- [ ] **Step 10: Vérifier de bout en bout**

```bash
curl -sI https://feeds.example.com/<préfixe>/all.xml | head -3
```
Expected: `200 OK` avec un content-type XML.

Puis ajouter `https://feeds.example.com/<préfixe>/<compte>` dans News Explorer : les images doivent s'afficher et une vidéo se lire en place.

- [ ] **Step 11: Planifier dans Unraid**

User Scripts → nouveau script pointant sur `run.sh`, cron `0 8,14,21 * * *`. Le décalage aléatoire interne fait le reste.

- [ ] **Step 12: Commit final**

```bash
git add deploy/run.sh deploy/Caddyfile.snippet tests/test_parse_real.py tests/fixtures/real_profile.json
git commit -m "🚀 chore: deployment scripts and real-capture parser fixture"
```

---

## Self-Review

**Couverture de la spec.** §4.1 composants → Tasks 11-12. §4.2 arborescence → Tasks 1, 5, 6. §4.3 flux d'une session → Task 10. §4.4 modules → Tasks 1-10. §5.1 session → Task 9. §5.2 amorçage → Tasks 10 (`--login`), 11 (noVNC). §5.3 rythme → Tasks 9-10. §5.4 interception et archivage brut → Tasks 6, 9, 10. §5.5 blocage → Tasks 4, 5, 9, 10. §6 modèle de post → Task 2. §7.1 RSS → Task 7. §7.2 Caddy → Task 12. §7.3 préfixe → Tasks 1, 7, 12. §8 notifications → Tasks 8, 10. §9 tests → chaque tâche. §10 étapes manuelles → Task 12. §12 risques → mitigations en Tasks 4, 5, 12 Step 6. Aucune section sans tâche.

**Cohérence des types.** `Config` est construit en Task 1 et consommé à l'identique en 7, 9, 10. `Post` / `Media` traversent 3, 6, 7, 10 avec les mêmes champs, `Media.path` passant de `None` à un chemin relatif uniquement dans `Store.save_post`. `Store` accumule ses méthodes en 5 puis 6 sans renommage. `BlockedError` est levée dans `collect_payload` (9), stockée par le gestionnaire d'événement, relancée par `warm_up`/`visit_profile`, et rattrapée dans `run_session` (10). `detect_block` (4) n'a qu'un seul appelant, `collect_payload`.

**Deux corrections issues de cette relecture.** Playwright avale les exceptions levées dans un gestionnaire `page.on("response")` : `Session` mémorise donc le blocage et le relance depuis `visit_profile`, sinon la détection de blocage n'aurait jamais atteint `run_session` et tout le dispositif de sécurité aurait été décoratif. Et `warm_up()` peut elle aussi rencontrer un blocage — le fil d'accueil est la première requête de la session — donc `run_session` l'entoure du même traitement que les visites de profil.

**Écart assumé.** Les fixtures des Tasks 3-4 sont synthétiques ; la Task 12 Step 6 les confronte au réel et corrige `parse.py`. Le compte dédié n'étant pas utilisable avant la fin de son réchauffement, attendre une capture réelle bloquerait les onze autres tâches.
