# instascrap — design

> **Statut** : validé en conception, non implémenté
> **Date** : 2026-09-04
> **Hôte cible** : tower (Unraid 7.3, x86_64)

## 1. Objectif

Suivre ~20 comptes Instagram publics sans jamais ouvrir Instagram : chaque compte
devient un flux RSS servi en HTTPS, lu depuis News Explorer (Mac, iPhone, iPad via
iCloud). Les médias sont réhébergés localement et archivés indéfiniment.

Le succès se mesure à trois choses : aucun post manqué, aucun média cassé dans le
lecteur, et un compte Instagram qui ne se fait pas bloquer.

## 2. Contexte — pourquoi pas plus simple

> [!important]
> Ce projet existe parce que les deux approches évidentes échouent. Le documenter
> évite de les retenter dans six mois.

**instaloader et tout client HTTP équivalent** se font bloquer. Le blocage ne porte
pas sur le protocole — instaloader fait déjà du HTTPS vers les mêmes endpoints que le
navigateur — mais sur l'empreinte complète : TLS (JA3/JA4) d'OpenSSL au lieu de celui
d'un navigateur, HTTP/1.1 au lieu de HTTP/2, en-têtes `x-ig-www-claim` non tenus à
jour, absence de JS et d'assets, et un motif de requêtes sans temps humain. Vérifié en
session : compte perso passé en `feedback_required` (action block) dès la première
requête après import de cookies, et IP en 401 en anonyme.

**L'API Graph officielle** (`business_discovery`) est gratuite et ne se fait jamais
bloquer, mais ne couvre pas le besoin : cibles limitées aux comptes Business/Creator,
pas d'historique paginable, ni stories ni commentaires, et une App Review Meta qui
rejette précisément les usages de collecte sur des tiers.

D'où le choix d'un vrai navigateur piloté.

## 3. Décisions

| Décision | Choix | Pourquoi |
|:---|:---|:---|
| Moteur | Playwright + Chrome réel (`channel="chrome"`) | Empreinte TLS/H2 authentique, JS exécuté, en-têtes corrects produits par la page |
| Hôte | tower, conteneur Docker | Même IP publique résidentielle que le Mac (vérifié), toujours allumé, 1,7 To libres |
| OS du navigateur | Linux, sans usurpation | Une plateforme minoritaire mais **cohérente** est moins suspecte qu'un Windows usurpé trahi par `navigator.platform`, WebGL et les polices |
| Compte IG | Nouveau compte dédié, réchauffé ~1 semaine | Le compte perso n'est jamais exposé ; un compte grillé se remplace |
| Collecte | Rotation de profils, ~7 par session | Le grid d'un profil est exhaustif et chronologique — condition de l'objectif « aucun post manqué » |
| Fréquence | 3 sessions/jour, horaires ±30 min aléatoires | Rythme humain plausible, latence max 8 h, compteurs de rate-limit retombés entre deux passages |
| Rétention | Archive permanente, RSS sur 50 items | Le disque le permet ; le flux reste léger |
| Protection | Préfixe de chemin non devinable | Seule protection qui n'aille pas casser les médias (voir §7.3) |

## 4. Architecture

### 4.1 Composants

Un seul élément nouveau : le conteneur `instascrap`, basé sur
`mcr.microsoft.com/playwright:<version épinglée à l'implémentation>-noble`. Il tourne en **one-shot** — démarre, fait
une session, écrit, s'arrête. Pas de démon.

Déclenchement par **User Scripts d'Unraid**, trois créneaux quotidiens, chacun décalé
d'un délai aléatoire de 0 à 30 minutes tiré au lancement.

Caddy et CloudflaredTunnel existent déjà et ne sont que configurés.

### 4.2 Arborescence

Volume de données `/mnt/user/appdata/instascrap/` :

```
config.toml                           comptes suivis, préfixe public, fenêtre RSS, cadence
profile/                              profil Chrome persistant (cookies, device IDs, historique)
state.json                            dernier shortcode et date de visite par compte, blocked_until
raw/<compte>/<horodatage>.json.gz     payloads Instagram bruts, archivés
posts/<compte>/<shortcode>.json       métadonnées normalisées, jamais retouchées après écriture
media/<compte>/<shortcode>/           images et vidéos téléchargées
```

Cette arborescence est la **source de vérité**. Le RSS n'en est qu'une projection,
régénérable intégralement à tout moment.

La publication se fait par écriture des XML dans la racine Caddy existante
(`/mnt/user/appdata/caddy/site/instacrap/<préfixe>/`) et par **liens durs**
pour les médias — même inode, zéro duplication, archive indépendante du publié.

### 4.3 Flux d'une session

```
cron → verrou → lecture state.json → si blocked_until dans le futur : sortie immédiate
     → session Chrome sur profil persistant
     → fil d'accueil, 2-3 scrolls (camouflage, rien n'est extrait)
     → pour chacun des 7 comptes les plus anciennement vus, ordre mélangé :
         navigation profil → interception des réponses JSON → archivage brut
         → parse → diff avec le dernier shortcode connu
         → pour chaque post nouveau : téléchargement médias + écriture JSON
         → pause aléatoire 25-90 s
     → régénération des XML concernés + all.xml → liens durs → fin
```

### 4.4 Modules

| Module | Rôle | Dépend de |
|:---|:---|:---|
| `browser.py` | session Chrome, navigation, interception réseau | Playwright |
| `parse.py` | JSON brut → modèle de post normalisé | rien (fonction pure) |
| `store.py` | état, déduplication, écriture archive et médias | système de fichiers |
| `feed.py` | archive → RSS | `store` |
| `main.py` | orchestration, rythme, blocages, notifications | tous |

> [!note]
> `parse.py` est délibérément pur et sans I/O. C'est le module qui cassera quand
> Instagram changera la forme de ses réponses ; le tester sur fixtures permet de le
> réparer sans lancer un navigateur, et de re-parser tout l'historique archivé.

## 5. Le collecteur

### 5.1 Session

`launch_persistent_context()` sur `/data/profile`, `channel="chrome"`,
`headless=True`, résolution plausible, `locale="fr-FR"`, `timezone_id="Europe/Paris"`
— cohérents avec une IP française. Un fichier verrou empêche le chevauchement de deux
sessions.

### 5.2 Amorçage

Mode `--login` : le conteneur démarre avec Xvfb et noVNC exposé sur le LAN. Connexion
manuelle depuis le Mac dans un vrai Chrome, 2FA compris. Le profil est écrit sur le
volume ; toutes les sessions suivantes tournent en headless **sur ce même profil**.

### 5.3 Rythme

- Fil d'accueil : 2-3 scrolls, pauses 2-6 s.
- Entre profils : pause aléatoire 25-90 s.
- Dans un profil : la réponse du premier chargement suffit ; on ne pagine que si le
  dernier post connu n'y figure pas — donc jamais en régime établi.
- Durée typique d'une session : ~10 minutes.

### 5.4 Interception

`page.on("response")`, filtre sur `/api/v1/` et `/graphql/query` en `application/json`.
Les payloads bruts sont archivés compressés **en plus** des posts normalisés.

Les médias sont téléchargés via `context.request.get()` — la requête part du contexte
du navigateur avec ses cookies et ses en-têtes, jamais d'un client HTTP tiers.

### 5.5 Détection de blocage

> [!warning]
> C'est le point critique. Le compte perso a été grillé parce qu'instaloader a
> **insisté** après le premier refus. Le comportement inverse est ici non négociable.

Signaux surveillés : HTTP 401 et 429, JSON contenant `feedback_required` ou
`challenge_required`, redirection vers `/accounts/login` ou `/challenge/`.

Réaction : **arrêt immédiat de la session**. Pas de retry, pas de profil suivant.
Écriture de `blocked_until` dans `state.json` et sortie. Le cron suivant lit ce champ
et se termine sans ouvrir Chrome. Premier blocage : 24 h. Deuxième consécutif : 72 h.

## 6. Modèle de post

```
shortcode, account, timestamp_utc, caption, permalink,
media: [{type: image|video, path, width, height, duration}],
is_carousel, like_count, comment_count   (compteurs au moment de la capture)
```

Le JSON brut d'Instagram ne franchit jamais la frontière de `parse.py`.

## 7. Publication

### 7.1 RSS

Un fichier par compte, régénéré intégralement depuis l'archive à chaque session, plus
un `all.xml` agrégé. Fenêtre de 50 items ; l'archive garde tout.

La description de chaque item est du HTML : images du carrousel en `<img>`, vidéos en
`<video controls poster preload="none">`. News Explorer étant basé sur WebKit, les
vidéos sont jouées en place ; `preload="none"` évite de tirer des dizaines de Mo à
l'ouverture d'une liste. Un `<enclosure>` porte le média principal.

### 7.2 Caddy

```
feeds.example.com {
    root * /usr/share/caddy/instacrap
    @feed path_regexp feed ^/<préfixe>/([A-Za-z0-9._]+)$
    rewrite @feed /<préfixe>/{re.feed.1}.xml
    file_server
    import security_headers
}
```

Statique, sans reverse_proxy ni port supplémentaire — c'est le seul bloc du Caddyfile
qui ne soit pas un proxy, à dessein.

### 7.3 Protection

Un préfixe de chemin aléatoire couvre uniformément flux et médias. Il est généré une
fois à l'initialisation (16 caractères hexadécimaux), stocké dans `config.toml`, et
repris tel quel dans le bloc Caddy. Le changer invalide tous les abonnements existants
— c'est le geste de révocation si une URL fuite.

> [!important]
> Toute authentification réelle (Cloudflare Access + token de service, ou basic auth)
> **casse les images** : le lecteur RSS ne propage pas les identifiants du flux aux
> ressources `<img>` embarquées. Le préfixe non devinable est la seule protection
> compatible avec l'objectif « aucun média cassé ».

Il arrête les crawlers et les scans d'URL, pas un attaquant ciblé — ce qui correspond
au risque réel.

## 8. Erreurs et observabilité

Déclenchent une notification Unraid via `/usr/local/emhttp/webGui/scripts/notify` :
un blocage détecté, un échec de login ou de session, une exception non rattrapée, et
l'absence de tout post nouveau sur **7 jours consécutifs** tous comptes confondus.

Une session sans nouveau post est normale et ne notifie rien — c'est le cas courant.
Le signal utile est le silence prolongé, qui traduit un parse cassé ou une session
morte, pas une semaine calme.

> [!note]
> Sans cela, une panne se découvre trois semaines plus tard en constatant des flux
> vides. L'échec doit être bruyant.

## 9. Tests

| Cible | Approche |
|:---|:---|
| `parse.py` | Fixtures JSON capturées en réel → posts attendus. Le cœur du filet de sécurité. |
| `store.py` | Idempotence : rejouer deux fois une session ne produit ni doublon ni retéléchargement. |
| `feed.py` | Round-trip `feedparser` + validation du XML produit. |
| `browser.py` | Pas de test unitaire (intégration par nature). Mode `--dry-run` sans écriture, et mode rejeu des payloads archivés pour tester la chaîne sans navigateur. |

## 10. Étapes manuelles

1. Créer le compte Instagram dédié et le réchauffer ~1 semaine (usage manuel quotidien,
   follow des 20 comptes cibles).
2. Ajouter le hostname public `feeds.example.com` dans le dashboard Cloudflare — le
   tunnel est piloté par token, sa configuration n'est pas dans un fichier local.
3. Régénérer le token du tunnel : il a été affiché en clair pendant la conception.
4. Connexion Instagram initiale via le mode `--login` (noVNC).

## 11. Hors périmètre

Stories, highlights, posts tagués, commentaires, hashtags, comptes privés,
purge automatique (à ajouter seulement si le disque devient un sujet), interface web.

## 12. Risques connus

| Risque | Mitigation |
|:---|:---|
| Instagram change la forme de ses réponses | `parse.py` isolé et testé sur fixtures ; payloads bruts archivés pour re-parser |
| Le compte dédié se fait bloquer | Arrêt immédiat et backoff ; le compte est jetable et remplaçable |
| Détection de l'automatisation évolue | Approche valable des mois, pas des années ; migration possible vers VM Windows si l'OS devient le facteur limitant |
| Déconnexion / challenge périodique | Quelques interventions manuelles par an via le mode `--login` |
