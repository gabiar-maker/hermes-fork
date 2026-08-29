# jb_outbound — « rien ne part sans accord » (Jean-Billie)

Greffe Jean-Billie sur **Hermes Agent** (Nous Research, MIT). Un seul plugin, **zéro patch du
scheduler cron** depuis F2 (2026-08-29 : `cron/scheduler.py` est byte-identique à l'amont, le
pont d'attribution passe par les hooks natifs `on_session_start` / `on_session_end`) → suivi de
l'upstream trivial.

## Ce que ça fait

Tout envoi sortant que l'assistant tente — message **Telegram** (`send_message`) ou **email / réseau
social** via **Composio** (`mcp__composio__*`) — est **intercepté** et transformé en **proposition à
valider**. L'envoi réel n'a lieu qu'**après l'accord du client**.

Les deux familles d'envoi passent par le même `tool_execution` middleware (Hermes) → un seul point
de greffe couvre tous les canaux et tous les déclencheurs (cron, chat, Telegram, email entrant).

## Boucle

```
outil d'envoi appelé
   → middleware : court-circuit (l'outil NE s'exécute PAS)
   → enregistre l'envoi (args complets) dans ~/.hermes/jb_pending/{jb_id}.json   (local, jamais relayé)
   → POST DraftRequest → http://127.0.0.1:8442/v1/draft   (daemon → proposition « pending »)
   → rend au modèle : « préparé, rien ne part tant que ce n'est pas validé »

[client valide dans son espace]

   → daemon pousse la DecisionItem → http://127.0.0.1:8444/jb/decision   (listener du plugin)
   → replay : registry.dispatch(tool_name, args)   (envoi RÉEL — ne repasse pas par le middleware)
   → POST ResultRequest {id, executed|failed} → http://127.0.0.1:8442/v1/result
```

## Attribution (départements) & fil d'activité

À l'ouverture de la session d'un job cron, le cœur tire le hook natif `on_session_start`
(`platform == "cron"`, `session_id = cron_{job_id}_{YYYYmmdd_HHMMSS}`) : le plugin en extrait
`job_id`, lit le job dans le store cron (`cron.jobs.get_job`, lecture seule) et pose le **contexte
d'attribution** (`job_context.py`) : casquette lue dans le front-matter du skill du job
(`casquette:` pour les skills gold, `department:` pour les customs), id du skill, id du job.
`on_session_end` signale la fin et nettoie.

- **Registre par session, pas de ContextVar** : ces deux hooks sont BORNÉS par le cœur (exécutés
  dans un thread de travail sur une copie du contexte), une ContextVar posée là serait invisible
  du thread de l'agent. Le middleware reçoit `session_id` / `turn_id` du cœur à chaque appel
  d'outil et retrouve le contexte par `session_id` ; il pose un alias `turn_id → session_id` au
  premier appel d'outil, qui survit à la rotation de session à la compression.
- **Stamp des drafts** : tout DraftRequest émis pendant un job porte les champs additifs de
  premier niveau `department`, `skill_id`, `job_id` (omis hors contexte job — chat libre). Le
  daemon ignore les champs inconnus tant que le contrat Go n'est pas étendu (vague 2).
- **Fil d'activité** (`activity.py`) : au début et à la fin de chaque job cron, POST
  fire-and-forget `http://{JB_DRAFT_ADDR}/v1/activity` avec
  `{phase: "started"|"finished", status: "ok"|"error", department?, skill_id?, job_id?, label?}`
  (`label` = nom lisible du job ; `status` = issue du tour : `completed` sans `failed` ni
  `interrupted`). **Gated par `JB_ACTIVITY_EVENTS=1`** (défaut OFF — la route daemon n'existe
  pas encore). Timeout 2 s, échecs avalés : ne bloque jamais un job.
- **Limites (vs l'ancien wrapper `run_job`)** : un job qui échoue AVANT de créer l'agent (script
  sans sortie, blocage par le scanner d'injection cron, préflight) n'ouvre aucune session → aucun
  signal ; l'issue de la livraison post-tour n'est pas reflétée ; une compression AVANT tout appel
  d'outil (théorique) perdrait l'attribution après rotation.

## Commande « jb-mcp-probe » (sonde MCP du fleet daemon)

`hermes jb-mcp-probe --url <url> [--header "Name: Value"]...` : découverte `tools/list` d'un
serveur MCP ad hoc, **read-only** (jamais `tools/call`, rien n'est écrit dans `config.yaml`),
stdout = un seul objet JSON `{"tools": [{"name", "description"}]}`, diagnostics sur stderr sans
jamais renvoyer l'URL ni les en-têtes, codes de sortie 2 (usage) / 1 (sonde en échec). Enregistrée
par `ctx.register_cli_command` (`mcp_probe.py`) ; sortie **identique** à `hermes mcp probe` (hunk
cœur en transition : il est retiré quand le daemon Go bascule de chaîne — lane D).

## Outil « creer_support » (Ma marque, Option A)

Le plugin enregistre aussi un **outil** (`ctx.register_tool`, zéro patch du cœur) : `creer_support`.
Quand le client demande un support (« fais-moi un carrousel », un devis, une présentation…), l'agent
émet une INTENTION structurée `{ type, contenu }` — il ne dessine jamais lui-même. Le POST part sur
le **loopback du daemon** (`http://{JB_DRAFT_ADDR}/v1/produce`), qui le relaie au control-plane avec
son identité mTLS (même chemin que les drafts / `request_tool_connection`) ; la plateforme rend le
support de façon **déterministe** (gabarits fixes, charte du client) et le range dans l'Espace
Documents. L'URL signée revient à l'agent, qui la partage **telle quelle** dans la conversation.

- 9 familles (enum fermé) : `presentation`, `devis`, `facture`, `post`, `carrousel`, `story`,
  `prospectus`, `signature`, `lettre` — contenu re-validé/borné côté plateforme.
- **Purement interne** : le document est déposé chez le client, rien ne part vers un tiers.
  L'ENVOI ultérieur du document repasse par la boucle de proposition ci-dessus.
- **Gated** comme le reste du plugin (`JB_DECISION_PUSH_URL`) : hors box Jean-Billie, l'outil est
  invisible (`check_fn`). Relais indisponible → message franc, jamais de bluff.
- Toolset plugin `jb_studio` (activé par défaut sur toutes les plateformes, désactivable via
  `hermes tools`).

## Outil « request_tool_connection » (demander un outil manquant) — greffe F2

Vivait dans `tools/request_tool_connection.py` (fichier additif du cœur) ; depuis F2 (2026-07-09)
le module vit ICI (`request_connection.py`) et s'enregistre par le même seam (`ctx.register_tool`).
Quand il MANQUE un outil pour accomplir une demande, l'agent envoie une INTENTION en langage
naturel au daemon loopback (`/v1/request-connection`) ; le control-plane répond en white-label
(souvent un lien de branchement self-service que le CLIENT clique lui-même — rien ne part vers un
tiers).

- Toolset **« messaging » conservé à l'identique** : c'est l'entrée explicite de l'allowlist
  `platform_toolsets` émise par le bundle (lane S, monorepo) qui expose l'outil — toolset
  REGISTRE (aucune entrée statique dans `toolsets.py`), résolu dynamiquement. Ne pas le renommer
  sans synchroniser le config-generator du monorepo.
- Gated `JB_DECISION_PUSH_URL` (`check_fn`) ; daemon injoignable → réponse franche `unavailable`.

## Aux task « goal_judge » (juge de mission) — greffe F2

Le juge DONE/CONTINUE des missions de fond est **natif** (`hermes_cli/goals.py`) ; seule sa
CONFIG l'était par un bloc `DEFAULT_CONFIG.auxiliary.goal_judge` patché dans le cœur. Depuis F2,
le plugin la déclare via `ctx.register_auxiliary_task("goal_judge", defaults={…})` : le pont natif
fusionne ces defaults SOUS `config.yaml auxiliary.goal_judge` (l'opérateur garde la main), et la
tâche apparaît dans le picker « Configure auxiliary models ». Defaults = valeurs neutres alignées
sur les fallbacks natifs (`provider: auto`, `max_tokens: 4096`, `timeout: 30`) — comportement
identique avec ou sans plugin.

## Règles

- **Fail-closed** : un outil d'envoi composio non répertorié est **bloqué** (jamais auto-envoyé). On
  élargit les listes dans `classify.py` au besoin.
- **Fail-closed aussi sur nos propres pannes** : le runner de middleware du cœur est fail-open (une
  callback qui lève avant `next_call` → l'outil est exécuté). Toute la décision du middleware est
  donc encadrée d'un `try/except` global : si la classification, le mapping, le store ou la
  construction du brouillon échouent, le modèle reçoit un résultat `blocked` (« rien n'est parti »)
  et l'outil **ne s'exécute pas** — journalisé en ERROR avec le nom de l'outil et le type d'erreur,
  jamais les arguments. Le pass-through des outils non concernés (lecture, interne) est inchangé.
- **Déchargement propre** : le listener enregistre son arrêt (`shutdown()` + `server_close()`) via
  `ctx.on_unload` quand le cœur l'offre (Hermes ≥ 0.20 : cache des plugins par profil,
  rechargement forcé) — garde `getattr` : le plugin charge aussi sur 0.18.2, où `on_unload`
  n'existe pas.
- **Asynchrone** : l'envoi est rejoué hors du run d'agent (le store survit au redémarrage,
  idempotent sur `jb_id`). Pas de blocage du run en attendant la validation humaine.
- **Minimisation** : les arguments complets (corps, destinataire détaillé) restent **locaux**
  (`jb_pending/`). Le `DraftRequest` ne porte que kind / titre / aperçu / destinataire d'affichage.
- **Loopback only** : le listener bind strictement `127.0.0.1` (garde-fou symétrique du daemon).

## Activation

Opt-in via la config Hermes :

```yaml
plugins:
  enabled: [jb_outbound]
```

Endpoints lus dans l'environnement (posés par le bundle Jean-Billie / le `daemon.env`) :
`JB_DRAFT_ADDR` (défaut `127.0.0.1:8442`), `JB_DECISION_PUSH_URL` (défaut
`http://127.0.0.1:8444/jb/decision`), `JB_ACTIVITY_EVENTS` (`1` pour activer le fil d'activité,
défaut OFF). Sans `JB_DECISION_PUSH_URL`, le plugin reste **passif**.

## Lancer / builder une box locale sous Windows

Deux pièges spécifiques à Windows, tous deux prouvés en local (P1) :

1. **`docker-compose.windows.yml` doit référencer l'image du fork, pas l'upstream.**
   C'est la variante du compose principal (`docker-compose.yml`) qui remplace
   `network_mode: host` par des `ports:` explicites — nécessaire car Docker Desktop
   pour Windows ne supporte pas le mode réseau host. Elle référence désormais
   `image: hermes-agent` (le tag construit localement, identique au compose
   principal), **pas** `nousresearch/hermes-agent:latest`. Si elle pointait vers
   l'image upstream, une box lancée avec ce fichier n'embarquerait PAS
   `plugins/jb_outbound/` : « rien ne part sans accord » serait absent,
   silencieusement (aucune erreur au démarrage — l'agent tournerait, juste sans
   le middleware de validation). Construisez l'image du fork avant de lancer
   ce compose (`docker compose build` via `docker-compose.yml`, ou
   `docker build -t hermes-agent .`).

2. **Builder l'image depuis un checkout Windows avec `core.autocrlf=true` casse le
   boot du conteneur.** Les scripts de service s6 (`docker/s6-rc.d/**/run`,
   `.../type`, …) n'ont pas d'extension et ne sont donc pas couverts par les
   règles `text eol=lf` de `.gitattributes` (qui ne visent que `*.sh` et
   `Dockerfile`). Avec `core.autocrlf=true`, le checkout Windows les convertit en
   CRLF ; le `Dockerfile` les copie tels quels (`COPY docker/s6-rc.d/ /etc/
   s6-overlay/s6-rc.d/`), et le conteneur crashe au boot avec :
   ```
   s6-rc-compile: fatal: invalid /etc/s6-overlay/s6-rc.d/dashboard/type
   ```
   **Contournement prouvé** : exporter les sources en LF avant de builder, sans
   toucher au checkout local —
   ```sh
   git -c core.autocrlf=false archive HEAD | tar -x -C /path/vers/export-lf
   cd /path/vers/export-lf && docker build -t hermes-agent .
   ```
   ou, plus simplement, builder depuis WSL (checkout Linux natif, pas de
   conversion de fin de ligne).

## Tests

`python -m pytest plugins/jb_outbound/` — autonome (mocke le HTTP loopback et le registre
d'outils, n'a pas besoin d'un environnement Hermes complet). Sous Windows :
`pytest -o addopts=""` (pytest-timeout/SIGALRM indisponible).
