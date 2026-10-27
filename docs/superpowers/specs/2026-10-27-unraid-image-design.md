# instascrap — image Docker partageable pour Unraid

> **Statut** : conception validée en discussion, pas encore implémentée
> **Date** : 2026-10-27
> **Remplace** : le déploiement par User Scripts décrit dans `docs/DEPLOYMENT.md`
> (`deploy/run.sh`, `deploy/login.sh`, `config.toml`)

## 1. Intention

Aujourd'hui instascrap ne fonctionne que grâce à ce qui l'entoure sur tower :
un cron User Scripts, `run.sh` (délai aléatoire, verrou, alertes Discord via un
script maison de l'hôte), `login.sh` lancé à la main en SSH, un Caddy externe qui
sert le dossier publié, et un `config.toml` édité à la main. Rien de tout ça ne se
transmet à quelqu'un d'autre.

Le but est une image Docker autonome qu'un utilisateur Unraid installe depuis un
template, règle entièrement dans le formulaire Unraid, et n'a plus jamais besoin
d'ouvrir un terminal pour faire tourner : le conteneur planifie ses passages, sert
ses flux, détecte une session Instagram expirée, ouvre lui-même l'écran de
connexion et prévient sur Discord.

Public visé : des utilisateurs Unraid qui ont un reverse proxy (SWAG, Nginx Proxy
Manager, Caddy…) ou vont en installer un, et qui en attendant passent par l'IP
locale. Le dépôt GitHub sera rendu public plus tard ; l'image, elle, peut être
publique dès maintenant.

## 2. Objectifs, non-objectifs, anti-objectifs

**Objectifs**

- Un conteneur longue durée, un seul dossier `/data`, deux ports, configuré
  uniquement par variables d'environnement.
- Planification, service HTTP des flux, cycle de connexion et alertes internes au
  conteneur.
- Template Unraid (XML) et README GitHub en anglais.
- Image publiée sur `ghcr.io/imthai/instascrap` par GitHub Actions.
- tower migré sur cette même image, **sans changer une seule URL de flux** :
  les abonnements News Explorer existants doivent continuer à fonctionner.

**Non-objectifs**

- Publication sur Community Apps (décidé : non).
- HTTPS et nom de domaine : c'est le travail du reverse proxy de l'utilisateur.
- Notifications natives Unraid : pas d'accès propre depuis un conteneur.
- Nettoyage de l'historique Git avant le passage du dépôt en public : sujet
  séparé, traité au moment de l'ouverture.
- Support multi-comptes Instagram ou multi-instance dans un même conteneur.

**Anti-objectifs** (ce qui serait un échec même si « ça marche »)

> [!warning]
> - noVNC joignable sans mot de passe, ou démarré en permanence.
> - Un mot de passe Instagram stocké où que ce soit (variable, fichier, log).
> - Des réglages par défaut plus agressifs qu'aujourd'hui envers Instagram.
> - Un conteneur qui insiste après un blocage ou une déconnexion (la logique de
>   backoff existante doit être conservée telle quelle).
> - Une liste des fichiers servie par le serveur web : elle révélerait le préfixe
>   secret.
> - Une rupture des URL publiques de tower lors de la migration.

## 3. Configuration

Tout passe par des variables d'environnement. Le `config.toml` disparaît de
l'image. La validation actuelle de `config.py` (charset des comptes, `.`/`..`
refusés, préfixe en 16 hex minuscules, `min ≤ max`) est conservée, appliquée aux
variables ; une erreur de config fait échouer le démarrage avec un message
explicite dans les logs (pas de boucle de redémarrage silencieuse).

### 3.1 Champs visibles du template

| Variable / champ | Type   | Défaut                         | Rôle |
|:-----------------|:-------|:-------------------------------|:-----|
| `ACCOUNTS`       | Variable, obligatoire | —                | comptes séparés par des virgules ; espaces tolérés autour |
| `PUBLIC_URL`     | Variable, obligatoire | —                | URL de base des flux telle que vue par le lecteur RSS (`https://feeds.example.com` ou `http://192.168.1.10:8080`) ; `/` final retiré |
| `VNC_PASSWORD`   | Variable, obligatoire, masquée | —       | mot de passe de l'écran de connexion noVNC |
| `SCHEDULE`       | Variable | `08:00,14:00,21:00`          | heures des passages, dans `TZ` |
| `/data`          | Chemin   | `/mnt/user/appdata/instascrap` | tout l'état persistant |
| `8080`           | Port     | `8080`                       | flux, médias, page récapitulative |
| `6080`           | Port     | `6080`                       | noVNC, ouvert seulement pendant une connexion |

`TZ` n'est pas un champ du template : Unraid l'injecte dans tous les conteneurs
à partir du fuseau du serveur, avec `HOST_HOSTNAME`, `HOST_OS` et
`HOST_CONTAINERNAME` (vérifié le 2026-10-27 sur le conteneur `navidrome` de
tower : `TZ=Europe/Paris`, `HOST_HOSTNAME=Tower`). Défaut en dehors
d'Unraid : UTC.

### 3.2 Champs avancés (masqués par défaut dans le template)

| Variable | Défaut | Rôle |
|:---------|:-------|:-----|
| `ACCOUNTS_PER_SESSION` | `7`  | profils visités par passage |
| `FEED_ITEMS`           | `50` | articles max par flux et dans `all.xml` |
| `MIN_GAP_SECONDS`      | `25` | pause aléatoire min entre deux profils |
| `MAX_GAP_SECONDS`      | `90` | pause aléatoire max |
| `JITTER_MINUTES`       | `30` | décalage aléatoire max après chaque heure de `SCHEDULE` |
| `FEED_SECRET`          | vide | préfixe secret des URL ; vide = généré au premier démarrage (`secrets.token_hex(8)`) et conservé dans `/data/feed_secret` ; renseigné = doit valoir 16 hex et prime sur le fichier |
| `DISCORD_WEBHOOK`      | vide | alertes ; vide = logs uniquement |
| `PUID` / `PGID`        | `99` / `100` | propriétaire des fichiers écrits dans `/data` |

Les descriptions de chaque champ dans le template sont courtes ; le détail
(notamment la règle de fraîcheur, §8) est dans le README.

## 4. Disposition de `/data`

```
/data/
  profile/        session Chrome persistante (existant)
  posts/ media/ raw/ state.json last_run.json   (existant, inchangé)
  feed_secret     préfixe généré, si FEED_SECRET est vide
  public/<préfixe>/   flux, index.html, médias publiés (hardlinks)
```

`public/` remplace l'ancien `publish_dir` séparé. Être dans le même volume que
`media/` garantit que les hardlinks de `feed.publish` fonctionnent toujours ; le
repli par copie reste en place mais ne doit plus servir.

## 5. Processus dans le conteneur

Un seul processus Python principal (le planificateur), lancé derrière un init
minimal (`tini` ou équivalent) pour la gestion des signaux et des zombies. Il :

1. valide la configuration, prépare `/data` (droits PUID/PGID, `feed_secret`) ;
2. lance Caddy en processus enfant et le relance s'il meurt ;
3. régénère tous les flux (`publish`) au démarrage, pour qu'un changement de
   `PUBLIC_URL`, `FEED_ITEMS` ou de comptes soit visible sans attendre un passage ;
4. si aucune session valide n'existe, entre en mode connexion (§6) ;
5. sinon, attend le prochain créneau.

**Planification.** Pour chaque heure de `SCHEDULE`, un passage démarre à cette
heure plus un délai aléatoire uniforme dans `[0, JITTER_MINUTES]`. Un créneau
manqué (conteneur arrêté) n'est pas rattrapé. Pendant un backoff (§7), les
créneaux sont sautés avec une ligne de log. Passage et connexion ne tournent
jamais en même temps : c'est le même processus qui les ordonne, ce qui remplace
le verrou `session.lock` de `run.sh`/`login.sh`.

**Passage.** La logique actuelle de `_run` est reprise sans changement de
comportement (ordre de visite, pauses, détection de grille, archivage, publish).

**Caddy.** Binaire officiel copié dans l'image. Sert `/data/public` en HTTP sur
`:8080`, sans `browse`, avec les en-têtes déjà utilisés sur tower
(`X-Robots-Tag: noindex, nofollow`, `Referrer-Policy`). Les requêtes Range doivent
fonctionner (lecture vidéo avec avance rapide). La racine `/` et tout chemin hors
du préfixe renvoient 404.

## 6. Cycle de connexion

**Déclencheurs** : aucune session au démarrage, ou un passage qui tombe sur une
redirection vers `/accounts/login` ou `/challenge`.

**Déroulé** :

1. les passages sont suspendus ;
2. démarrage de Xvfb, x11vnc (avec `VNC_PASSWORD`) et websockify sur `:6080`, puis
   d'un Chrome visible sur le même profil persistant, ouvert sur la page de
   connexion Instagram (même chaîne que `login.sh`, mêmes attentes bornées) ;
3. log et alerte Discord : « Instagram session expired — log in at
   `http://<hôte>:6080/vnc.html` » (au premier démarrage : « no Instagram session
   yet »). L'hôte est `HOST_HOSTNAME` (injecté par Unraid, §3.1) ; hors Unraid,
   le message dit littéralement « your server's IP ». Le nom d'hôte ne se résout
   pas sur tous les réseaux locaux, donc le message rappelle aussi que l'IP du
   serveur fonctionne ;
4. le conteneur sonde la session (toutes les quelques secondes) jusqu'à ce qu'elle
   soit valide : cookie `sessionid` présent **et** URL de la page hors de
   `/accounts/login` et `/challenge`. La sonde ne doit jamais naviguer elle-même :
   la page est sous les doigts de l'utilisateur, une navigation au milieu de sa
   saisie ou de son code 2FA ferait échouer la connexion ;
5. session valide : Chrome, websockify, x11vnc et Xvfb sont arrêtés, le port 6080
   ne répond plus, alerte « logged in again », les passages reprennent au prochain
   créneau ;
6. tant que la connexion n'est pas faite, l'alerte est renvoyée une fois par jour.

La protection existante de `--login` contre un blocage survenu pendant la saisie
(`raise_if_blocked` après la connexion) est conservée : un `challenge_required`
pendant la connexion entraîne le backoff, pas une reprise immédiate.

Distinction avec le blocage : **déconnecté** = redirection vers
`/accounts/login` ou `/challenge` → mode connexion, sans backoff ;
**freiné** = HTTP 429/401, `feedback_required`, `challenge_required` dans une
réponse → backoff (§7). Aujourd'hui tout passe par `detect_block` et le même
backoff ; il faut séparer les deux catégories sans affaiblir la détection.

## 7. Alertes

Envoyées en anglais par le conteneur lui-même : POST JSON vers `DISCORD_WEBHOOK`
(embed avec couleur selon la gravité), via la bibliothèque standard ou une
dépendance déjà présente. Un échec d'envoi est loggé et n'interrompt jamais un
passage. Sans webhook : logs uniquement. Reprise des alertes de `run.sh` :

| Événement | Gravité | Fréquence |
|:----------|:--------|:----------|
| Session expirée, connexion requise | alerte | à l'entrée en mode connexion, puis 1×/jour |
| Reconnecté | info | une fois |
| Freiné par Instagram (backoff) | alerte | à chaque blocage, avec l'heure de reprise |
| Capture cassée (aucun profil visité n'a renvoyé sa grille) | alerte | à chaque passage concerné |
| Grille manquante sur une partie des profils | avertissement | à chaque passage concerné |
| Échecs isolés pendant un passage | avertissement | à chaque passage concerné |
| Silence (aucun nouveau post depuis N jours, logique `stale` existante) | alerte | à chaque passage concerné |
| Passage planté (exception) | alerte | à chaque fois |

`notify.py` (notifications Unraid, déjà mort en production) est remplacé.
`last_run.json` n'a plus de lecteur côté hôte ; le garder ou non est laissé au
constructeur.

## 8. README (anglais)

1. Ce que fait l'app, en une phrase, et ce qu'elle ne fait pas.
2. **Avertissement** : scraper enfreint les conditions d'Instagram, le compte
   utilisé peut être bloqué ; utiliser un compte dédié.
3. Installation sur Unraid : ajout du dépôt de templates dans les réglages Docker,
   ou copie du XML dans `/boot/config/plugins/dockerMan/templates-user/`.
4. Première connexion via noVNC, pas à pas.
5. Tableau de toutes les variables (§3), avec la règle de fraîcheur :
   visites/jour = nombre d'heures dans `SCHEDULE` × `ACCOUNTS_PER_SESSION` ;
   un compte est revisité tous les `comptes / visites par jour` jours ; une visite
   récupère les ~12 derniers posts, donc un post n'est perdu que si le compte en
   publie davantage entre deux visites. Repère : ~20 comptes pour une visite par
   jour avec les valeurs par défaut.
6. Reverse proxy : pointer sur `:8080`. **Ne jamais exposer `:6080`.**
7. Changement de `PUBLIC_URL` : flux régénérés au démarrage, mais les abonnés
   doivent se réabonner.
8. Alertes Discord, dépannage (logs, session expirée, blocage, capture cassée).

## 9. Distribution

- `Dockerfile` adapté (Caddy, tini, entrypoint planificateur).
- Workflow GitHub Actions : build et push sur `ghcr.io/imthai/instascrap` à chaque
  tag `v*` (tags `vX.Y.Z` et `latest`), plateforme `linux/amd64`. Le paquet ghcr est
  rendu public indépendamment du dépôt.
- Template `unraid/instascrap.xml` dans le dépôt, champs du §3, avancés marqués
  comme tels. Pas de `WebUI` : la page récapitulative est derrière le préfixe
  secret, que le template ne connaît pas, et noVNC ne tourne que pendant une
  connexion. Le premier démarrage écrit l'URL de la page récapitulative dans les
  logs.
- `docs/DEPLOYMENT.md`, `deploy/run.sh`, `deploy/login.sh`, `deploy/user-script.sh`
  et `config.example.toml` sont retirés ou remplacés une fois tower migré.

## 10. Migration de tower

1. Créer le conteneur depuis le template avec :
   `ACCOUNTS` = liste actuelle de `config.toml`,
   `PUBLIC_URL=https://feeds.example.com`, `FEED_SECRET=3f9a1c0b7d2e4a68`,
   `/data` = `/mnt/user/appdata/instascrap`, mêmes réglages avancés, et le
   webhook Discord du homelab.
2. Les données existantes sont reprises telles quelles ; elles appartiennent à
   root et doivent être passées à `PUID:PGID` (au démarrage par le conteneur ou une
   fois à la main).
3. Le dossier publié actuel (`/mnt/user/appdata/caddy/site/instacrap`) n'est
   plus utilisé : le premier `publish` reconstruit `/data/public`.
4. Caddy de tower : remplacer `root`/`file_server` du site `feeds.example.com`
   par un `reverse_proxy` vers le conteneur sur `:8080`.
5. Vérifier qu'une URL de flux et une URL de média existantes répondent à
   l'identique, puis désactiver l'entrée User Scripts.
6. La session Chrome dans `/data/profile` est réutilisée : pas de reconnexion
   attendue.

## 11. Laissé au constructeur

- Structure du code du planificateur et de l'entrypoint.
- Abandon des privilèges vers PUID/PGID (`gosu`, `setpriv`…) et ce que ça implique
  pour le bac à sable de Chrome.
- Mécanisme exact de sondage de session (§6.4) et intervalle.
- Couleurs et format des embeds Discord.
- Maintien ou non de `last_run.json` et de `--capture`/`--republish` comme
  commandes manuelles (`docker exec`).
- Icône du template.
