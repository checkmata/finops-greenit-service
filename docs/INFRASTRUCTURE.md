# INFRASTRUCTURE — plateforme réelle (V2 → V4)

> Retour : [README docs](README.md) · Multicluster : [MULTICLUSTER](MULTICLUSTER.md) · Failover : [FAILOVER](FAILOVER.md)
>
> Tout ce qui est décrit ici a été **exécuté** sur un laptop Linux (Intel i5-1155G7, 8 threads, 16 Go RAM), les 28 et 29 septembre 2026.

## 1. Topologie exécutée

```text
Docker (hôte)
├── kind « primary »   (1 nœud, K8s 1.31)  kube-prometheus-stack · Kepler · banking · trading · Online Boutique (12)
├── kind « secondary » (1 nœud, K8s 1.31)  kube-prometheus-stack · Kepler · banking · Online Boutique en standby
│                                           + proxy basic-auth devant Prometheus (NodePort 30091)
└── docker compose (plan de gestion)
    ├── timescaledb (timescale/timescaledb:2.17.2-pg16) — migrations 001→004
    ├── api   (réplique 1)  ┐ même image, même base : élection de leader par
    ├── api-2 (réplique 2)  ┘ verrou consultatif PostgreSQL (profil "ha")
    └── grafana 11.1 — 5 dashboards provisionnés
```

Les API et Grafana rejoignent le réseau Docker `kind` : chaque nœud y est joignable par son nom (`primary-control-plane:30090`).

## 2. Pourquoi kind plutôt que minikube/KVM

Les deux ont été exécutés. Minikube/KVM a servi tant que Docker n'était pas accessible.

| Critère | minikube / KVM | **kind + Docker (retenu)** |
|---|---|---|
| Énergie Kepler | **estimée** (VM sans RAPL : `components_power_source="estimator"`) | **mesurée** : Kepler, privilégié, lit le RAPL de l'hôte (`rapl-sysfs`) |
| Mémoire | fixée par VM (5 + 4 Go) ; une VM de 3 Go a saturé | partagée avec l'hôte |
| Démarrage | 3–5 min par VM | ~1 min par cluster |
| Réseau entre clusters et Compose | réseaux libvirt isolés, problème de *hairpin* NodePort | un seul réseau Docker `kind` |
| Panne réaliste | suspendre une VM | `docker pause` du nœud (réversible) |
| Limite | — | les deux clusters partagent le **même hôte** (même package CPU pour RAPL) |

Le *timeout* du hook Helm observé sur la VM minikube secondaire a été **reproduit** sur la même VM : l'installation réussit (le hook se termine en ~10 s). La seule différence avec l'échec initial était la famine mémoire de l'hôte (~1 Go libre pendant la création d'une VM de 4 Go). Cause probable : l'hôte manquait de ressources. On ne peut pas le prouver formellement, car les événements avaient expiré et les ressources du hook sont supprimées par le chart.

## 3. Démarrer (commandes réellement utilisées)

```bash
# Clusters (chacun : monitoring, Kepler, images, workloads, pré-pull des images du standby)
scripts/k8s/kind-up.sh primary
scripts/k8s/kind-up.sh secondary

# Identités et secrets hors Git (.secrets/ ignoré)
scripts/k8s/make-actuator-secrets.sh secondary 24h   # token court (24 h par défaut) + CA ; le labo tourne avec 720 h (expire le 30/10/2026)
scripts/k8s/make-prometheus-auth.sh secondary        # proxy basic-auth + mot de passe généré

# Plan de gestion
cp .env.example .env   # puis choisir les mots de passe (dev)
docker compose -f docker-compose.yaml -f docker-compose.kind.yaml up -d --build
docker compose -f docker-compose.yaml -f docker-compose.kind.yaml --profile ha up -d   # 2e réplique
```

Accès : API http://127.0.0.1:8080 (réplique 2 : 8082) · Grafana http://127.0.0.1:3000 · Prometheus primary 127.0.0.1:19090, secondary 19091.

## 4. Ce qui a été vérifié, et comment

| Vérification | Commande / preuve | Résultat |
|---|---|---|
| Docker réel | `docker run --rm hello-world` | ✅ |
| Nœuds et pods | `kubectl --context kind-primary get pods -A` | ✅ tous `Running` : primary 31/31 et secondary 22/22 en V2 ; **35/35 et 39/39 au 30/09 21:20** (+ OCM, Argo CD, registre banking) |
| Charge réelle | PromQL CPU / mémoire / réseau par namespace | ✅ (banking, trading, ecommerce) |
| Kepler | `kepler_node_info{components_power_source="rapl-sysfs"}` | ✅ mesuré (package + DRAM) |
| TimescaleDB | `psql` : hypertables, rétention, 4 agrégats continus, 10 jobs | ✅ |
| Chaîne de bout en bout | `pytest -m infra` (Prometheus réels → cycle → TimescaleDB réelle) | ✅ |
| Grafana | `scripts/check_grafana_dashboards.py` exécute chaque requête de chaque panneau via Grafana ; captures d'écran | ✅ tous les panneaux avec des données réelles |
| HA | 2 répliques : 1 seul leader ; aucun cycle écrit deux fois ; reprise en ~2,8 s à l'arrêt du leader | ✅ |
| Failover réel | `scripts/k8s/failover-demo.sh` → [evidence/failover-demo-2026-09-29.log](evidence/failover-demo-2026-09-29.log) | ✅ |
| Prometheus authentifié | 401 sans / avec mauvais identifiants, 200 avec les bons ; le service passe par le proxy | ✅ |
| CI | workflow GitHub exécuté localement avec `act` (3 jobs) | ✅ |

Tests de l'infrastructure réelle :

```bash
eval "$(scripts/infra_env.sh)"          # INFRA_* (URLs + DB depuis .env, jamais affichées)
pytest -m infra -v                      # 40 tests réels (31 en V2)
```

## 5. Défauts trouvés GRÂCE à l'infrastructure réelle (tous corrigés et testés)

| # | Trouvé sur | Défaut | Correction |
|---|---|---|---|
| 1 | Kepler réel | métrique `kepler_container_joules_total` et label `container_namespace` ; `namespace` = pod Kepler | requêtes réécrites |
| 2 | Kepler (VM) | séries `mode="idle"` toujours à 0 en mode estimateur | idle = inconnu sauf puissance matérielle |
| 3 | Kepler (kind) | `joules_total` = RAPL + « other » **modélisé** (83 %) | mesuré = package + DRAM seulement, avec un `scope` |
| 4 | cAdvisor minikube | uniquement des séries au niveau pod | repli pod sans double comptage (`or`) |
| 5 | kube-state-metrics v2 | pas de `phase` sur `kube_persistentvolumeclaim_info` | `kube_persistentvolumeclaim_status_phase` |
| 6 | TimescaleDB | `timescaledb-tune` dimensionne sur la RAM de toute la machine → nœud saturé | `TS_TUNE_MEMORY`, `mem_limit` |
| 7 | test E2E | snapshot à 0 $ pour un namespace absent d'un cluster | namespace sauté |
| 8 | API réelle | `precision` ignorait la source de puissance | calculée sur attribution + source |
| 9 | Compose | plage de ports → l'API a été publiée sur 8081 | port fixe |
| 10 | Grafana | `${VAR:-défaut}` non supporté | variable explicite |
| 11 | Grafana (rendu) | coût de stockage « 0 » affiché pour un stockage `unavailable` | NULL |
| 12 | démo failover | 403 au milieu d'une promotion : les changements déjà faits n'étaient pas enregistrés → fail-back vide | progression partielle enregistrée, 403/404 = non répliqué |
| 13 | démo failover | images jamais tirées sur le standby → promotion bloquée plusieurs minutes | pré-pull dans `kind-up.sh` |
| 14 | HA | mise à jour depuis l'image V1 (sans verrou) : un cycle en double | documenté (mise à jour progressive depuis une version sans élection) |

## 5 bis. Ajouts V2.x · V4 (30/09/2026)

| Composant | Où | Rôle |
|---|---|---|
| Gitea 1.22 (`docker-compose.gitops.yaml`) | réseau `kind`, 127.0.0.1:3001 | dépôt GitOps local `finops/platform-gitops` (rien n'est poussé sur GitHub) |
| OCM 1.3 (`clusteradm`) | hub = `secondary` | ManagedClusters, Placement |
| Argo CD 3.5.3 core | hub = `secondary` | ApplicationSets `workloads`, `finops-platform` ; AppProject `finops` |
| `ledger-db` (PostgreSQL 16) | banking, les 2 clusters, NodePort 30432 | registre banking répliqué en flux |
| Identités | `.secrets/` (ignoré par Git) | Gitea admin, `finops-agent` (écriture sur un seul dépôt), registre, jeton de validation, actionneur |

Mémoire de l'hôte : ~300 Mo libres. D'où Argo CD en mode core, le hub sur `secondary` et pas de troisième cluster.

Hôte : `localhost` se résout en `::1` et le relais IPv6 de Docker se bloque après une recréation de conteneur. Utiliser **127.0.0.1**.

## 5 ter. État nominal vérifié (30/09/2026 21:20 UTC, audit en lecture seule)

| Élément | Observé | Statut |
|---|---|---|
| Nœuds kind | primary, secondary `Ready` ; 35 / 39 pods, aucun hors `Running`/`Completed` | VALIDATED |
| OCM | 2 ManagedClusters JOINED / AVAILABLE | VALIDATED |
| Argo CD | 4/4 pods ; 8/8 Applications Synced / Healthy, toutes sur la révision `3420b6f` = HEAD de Gitea | VALIDATED |
| Registre banking | `streaming` asynchrone, 0 octet de retard, 830 200 lignes des deux côtés, timeline 7 | VALIDATED |
| Service | primary actif, 1 leader (api-1) + 1 suiveur, `/health/ready` healthy, actionneur `ok` | VALIDATED |
| Anciennes VM minikube | `primary` / `secondary` **arrêtées** : 0 Mo de RAM, ~5,5 Go sur disque (`~/.minikube` 6,1 Go) ; utilisées seulement par `docker-compose.colocated.yaml` et `scripts/k8s/cluster-up.sh` (chemin V2) | REQUIRES USER ACTION (suppression facultative) |

## 6. Session Docker de cette machine

Claude Code a été démarré avant l'ajout de l'utilisateur au groupe `docker`. Un *shim* `~/.local/bin/docker` passe par `sg docker` tant que la session n'a pas le gid 131. Il est inutile après une reconnexion et peut être supprimé.
