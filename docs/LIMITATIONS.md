# LIMITATIONS — état réel après V2.x, V3, V4 (30/09/2026)

> Retour : [README docs](README.md) · Audit du MVP : [CODE_OBSERVATIONS](CODE_OBSERVATIONS.md) · Suite : [ROADMAP](ROADMAP.md)

## 1. Vérification sur infrastructure réelle (V2)

Tout ce qui était « non vérifié » en V1 a été **exécuté** en V2 (voir [INFRASTRUCTURE.md](INFRASTRUCTURE.md)) : Docker/Compose, 2 clusters kind, Kepler (mesuré RAPL), migrations 001→004 sur TimescaleDB 2.17, dashboards Grafana avec données réelles, labels Kepler réels, CI rejouée avec `act`.

Ce qui reste **non vérifié** :

| Élément | Pourquoi |
|---|---|
| Workflow CI sur GitHub même | branche non poussée ; rejoué localement avec `act` (3/3 jobs) |
| Clusters sur des hôtes distincts / cloud / OpenShift | un seul laptop : les deux clusters kind partagent l'hôte |
| TLS vers Prometheus / API K8s avec CA d'entreprise | code et tests unitaires ; en réel : HTTP + CA kind pour l'API K8s |
| Bearer token vers Prometheus (Thanos, OpenShift) | testé en unitaire/intégration ; le proxy réel utilise du basic auth |

## 2. Limites fonctionnelles restantes

**FinOps**
- Tarifs = constantes. Pas de facture cloud réelle ni de tarifs par nœud / classe de stockage.
- Pas de recommandation stockage ; coût du stockage `NULL` tant qu'aucun prix n'est configuré.
- Coût d'un workload = CPU + mémoire (le stockage reste au niveau du namespace).
- Capacité inutilisée des nœuds : non répartie.

**Green IT**
- Mesuré = package CPU + DRAM (RAPL). La part « other » de Kepler est **modélisée** : exclue du chiffre mesuré.
- Les deux clusters kind lisent le **même** RAPL de l'hôte : la mesure est réelle, mais l'attribution par nœud suppose l'isolation de Kepler.
- Sur VM (minikube) : `components_power_source="estimator"` → estimé, affiché comme tel.
- PUE et intensité carbone constants.

**Multicluster / failover**
- Actuation du failover limitée au scaling de Deployments **déjà déclarés** sur le standby ; depuis V2.x, GitOps y déclare banking, e-commerce **et trading**.
- Les données du registre banking sont répliquées et promues (V2.x). Le reste n'est pas répliqué, et le trafic n'est pas basculé : voir §2 bis.
- Déclarations opérateur et « dernière liste vue » encore en mémoire (l'état du contrôleur, lui, est en base).

**Recommandations**
- Règles V1 à seuils fixes, complétées par le ML V3 (percentiles, prévision, anomalies) et priorisées par l'agent V4 : voir §2 bis.

**Exploitation**
- HA par verrou consultatif PostgreSQL : nécessite PostgreSQL (SQLite → leader unique implicite).
- La mise à jour depuis une image V1 (sans élection) peut produire **un** cycle en double pendant la transition.
- Premier cycle après démarrage de Prometheus souvent vide ; rattrapé au cycle suivant.
- Démo locale : `MIN_OBSERVATION_HOURS=0.25` et `FAILOVER_MODE=auto` dans `.env` local ; les valeurs par défaut restent 24 h et `manual`.

## 2 bis. Limites V2.x / V3 / V4 (constatées, non masquées)

**Infrastructure**
- **Un seul hôte** (laptop, 16 Go, ~300 Mo de RAM libre) : les deux clusters, le hub GitOps, Gitea et le plan de gestion tombent ensemble en cas de panne matérielle réelle.
- Le **hub OCM / Argo CD est sur `secondary`**. Une panne de `secondary` coupe la gestion GitOps. Argo CD y dispose des droits in-cluster de son installation officielle, qui sont larges.
- Gitea est un point unique de défaillance et n'a pas de sauvegarde.
- RHACM **BLOCKED** (OpenShift requis) ; pas de politiques OCM (RAM).

**Données / DR**
- La réplication du registre est **asynchrone** : RPO = retard au moment de la panne (mesuré ≤ 0,08 s).
- **Fenêtre de split-brain** d'environ 1–2 s au retour de l'ancien primary : ~100 écritures acquittées, puis effacées à la resynchronisation. Pour la fermer, il faudrait une réplication synchrone ou STONITH.
- TimescaleDB du service, paniers (redis `emptyDir`) et carnet d'ordres trading : **non répliqués**.
- Pas de bascule du trafic (DNS / load balancer global).
- Le failback des données est une procédure scriptée déclenchée par un humain.
- Le jeton de l'actionneur de failover est porté à 720 h pour ce laboratoire. Il est relu à chaud, et `probe()` signale son expiration ; en production, il faudrait un jeton de ServiceAccount projeté.
- Un **incident réel non planifié** (09:48, 30/09) a déclenché un failover qui a échoué (jeton expiré) sans alerte : `/api/failover` expose désormais `actuators`, mais **aucune alerte n'est encore branchée** dessus.

**ML (V3)**
- 46 h d'historique : pas de saisonnalité, prévisions à 1 h, et sur 10/38 séries le modèle choisi fait pire que naive.
- Précision sur les anomalies réelles non mesurable (pas d'étiquettes).
- Les anomalies simultanées sont regroupées par l'agent, pas par le moteur ML.

**Agent (V4)**
- Validation par jeton partagé (pas de SSO ni de double validation) ; dry-run hors ligne (pas côté serveur, faute d'identité d'écriture sur le cluster, volontairement).
- Redimensionnement mono-conteneur ; gains énergétiques d'un redimensionnement non chiffrés (consolidation de nœuds non modélisée).
- Une action attend le sondage d'Argo CD (60 s) : 2–3 min au total.

## 2 ter. Liste consolidée (état vérifié le 30/09/2026)

| Limite | Statut |
|---|---|
| Tous les composants (2 clusters, hub GitOps, Gitea, plan de gestion) sur **un seul hôte physique** | PARTIAL : le DR est prouvé pour la panne d'un cluster, pas pour celle de l'hôte |
| Réplication PostgreSQL **asynchrone** | MEASURED : retard ≤ 0,08 s ; RPO nul non garanti |
| **Perte possible d'écritures** acquittées par l'ancien primary avant son isolement | MEASURED : ~100 lignes, isolement ≤ 1,7 s |
| TimescaleDB du service **non répliqué** | BLOCKED dans ce périmètre |
| Carnet d'ordres Trading **non répliqué** (mémoire) ; paniers e-commerce (`emptyDir`) | PARTIAL : seuls les manifests sont répliqués |
| **Trafic non basculé** (pas de DNS / load balancer global) | BLOCKED en local |
| **RHACM non utilisé** localement (OCM, son amont, à la place) | BLOCKED (OpenShift requis) |
| Validation humaine par **jeton partagé** (pas de SSO, pas de double validation) | PARTIAL |
| **Pas d'alerting** sur l'actionneur (`actuators`) ni sur la garde de données | PARTIAL : état exposé par l'API, rien ne déclenche d'alerte ; un incident réel (09:48) est passé inaperçu |
| Prévision : le modèle choisi sur la validation fait **pire que naive** sur 10/38 séries | MEASURED |
| Anomalies réelles : précision **non mesurable** sans étiquettes (41 épisodes, 4 liés à une opération) | PARTIAL |
| Monitoring (Helm) pas encore géré par Argo CD | PARTIAL |
| **Panne longue d'un cluster et GitOps** : jusqu'au 03/10, une panne de plus de ~4 min retirait le cluster de la décision OCM, et Argo CD supprimait ses workloads (incident réel du 01/10, [DR §5](DR.md)) | corrigé (`preserveResourcesOnDeletion` + tolérances OCM) ; **VALIDATED** par une panne réelle de 420 s ; non rejoué sans le correctif |
| Hôte saturé en mémoire : l'OOM killer a provoqué un failover réel non planifié le 01/10 | PARTIAL : l'hôte n'a que ~300 Mo libres ; aucune alerte branchée |
| Jeton de l'actionneur à 24 h (réémis le 03/10, expire le 04/10 15:23 UTC) et invalidé si son ServiceAccount est recréé | PARTIAL : `probe()` le signale (`unauthorized`) mais rien n'alerte ; à renouveler, ou à remplacer par un jeton projeté |
| Retour vers le primary du registre après l'incident du 01/10 non effectué : le service tourne sur `secondary` | REQUIRES USER ACTION (`scripts/dr/ledger-failback.sh`, efface l'ancienne copie du primary) |

## 2 quater. Sécurité du dépôt (audit du 30/09/2026)

- **Ancien mot de passe de développement** (analyse du 30/09/2026, valeur jamais affichée) :
  - **Ce que c'était** : le mot de passe de l'utilisateur PostgreSQL/TimescaleDB `finops` (base `finops_greenit`) du MVP.
  - **Où il apparaît** (introduit par `bb6052c`, puis `d8f23c2`) :
    - `docker-compose.yaml` (`POSTGRES_PASSWORD`) ;
    - `manifests/timescaledb-secret.yaml` (base64, qui n'est **pas** un chiffrement) ;
    - `manifests/secret.yaml` (`DATABASE_URL` en base64) ;
    - `app/config.py` (valeur par défaut de `DATABASE_URL`) ;
    - `.env.example` ;
    - `grafana/provisioning/datasources/datasources.yml` (source de données TimescaleDB de Grafana) ;
    - `scripts/smoke_test.ps1`.
  - **Encore présent** : dans l'historique, **et dans l'état actuel de `main`** (`22d2912`) tant que la branche `feature/v1-multicluster-workloads` n'est pas fusionnée. Une fusion simulée donne **0 fichier** contenant la valeur.
  - **Inactif** : absent de la branche, des fichiers de travail, de `.env` / `.secrets/`, des variables des conteneurs, des Secrets Kubernetes des 2 clusters et de la configuration Grafana en service. **Refusé** par la TimescaleDB en service (test d'authentification réseau : `InvalidPasswordError` ; témoins : un mot de passe aléatoire est refusé, le mot de passe actuel est accepté).
  - **Exposition** : le dépôt GitHub est **privé** (3 collaborateurs). Le risque est limité à ces accès, et à toute réutilisation de ce mot de passe hors de ce projet ; ne pas le réutiliser, et le changer partout où il aurait servi ailleurs.
  - Historique non réécrit, aucun force-push.
- **Index Git (corrigé le 30/09 avant le commit)** : l'index contenait les renommages `secret.yaml → secret.example.yaml` et `timescaledb-secret.yaml → timescaledb-secret.example.yaml` **avec l'ancien contenu** (les vraies valeurs). Ils ont été réindexés depuis l'arbre de travail assaini, et le contenu indexé a été vérifié (placeholders uniquement). `prometheus/prometheus.yml` est supprimé volontairement : remplacé en V1 par `prometheus-primary.yml` / `prometheus-secondary.yml`.
- Fichiers de travail : aucune valeur secrète en clair (recherche des valeurs réelles de `.secrets/` dans tous les fichiers suivis et non suivis). `.env`, `.secrets/`, `.gitops-work/` et `.demo/` sont ignorés par Git.

## 3. Statut des limites de l'audit MVP

| # (CODE_OBSERVATIONS) | Sujet | Statut V1 |
|---|---|---|
| 1 | scheduler limité à `default` | ✅ découverte + filtres include/exclude, multicluster |
| 2 | `cost_usd` toujours 0 | ✅ coût réel, réservé, gaspillage, économies historisés |
| 3 | plantage du repli DataQuality | ✅ corrigé (et repli = « rien de sain » au lieu de « tout sain ») |
| 4 | `/health` aveugle à Prometheus | ✅ `ping()` par cluster ; `/health/live` séparé |
| 5 | faux UUID dans l'historique | ✅ vrais ids + statut |
| 6 | transitions non contrôlées | ✅ règles dans le domaine, 409 sinon |
| 7 | idle toujours 0 | ✅ `None` si non mesurable ; plus d'ENERGY_WASTE sur donnée inexistante |
| 8 | stockage non valorisé | ✅ intégré avec `storage_status` |
| 9 | économies comptées deux fois | ✅ réservation libérée ; invariant testé |
| 10 | nom `cpu_core_hours` trompeur | ⚠️ nom conservé (compatibilité), sens documenté (cœurs moyens) |
| 11 | repli Kepler cluster entier | ✅ part CPU, marquée « estimée » |
| 12 | équipe = 1ʳᵉ série | ✅ majorité + détail par workload |
| 13 | équipe non stockée avec les recos | ✅ cluster, workload, équipe, centre de coût, environnement |
| 14 | adaptateurs créés à chaque requête | ✅ racine de composition, instances partagées |
| 15 | script de seed cassé | ✅ réécrit, idempotent pour les recos |
| 16 | `up` factice du faux exporteur | ✅ vrais jobs de scrape par composant |
| 17 | données locales à 0 | ✅ simulation à compteurs croissants |
| 18 | `/metrics` absent | ✅ métriques du service (scheduler, upserts) |
| 19 | réglages inutilisés | ✅ supprimés ou branchés (logging) |
| 20 | pas de dashboard | ✅ 5 dashboards, affichés avec données réelles (V2) |
| 21 | pas de README ni de CI | ✅ README + workflow (rejoué avec `act`, pas encore sur GitHub) |
| 22 | secrets de dev dans Git | ⚠️ PARTIAL : arbre de travail assaini (`*.example.yaml`, `.env` requis) ; **historique (`bb6052c`, dépôt privé) non réécrit** ; valeur inactive, retirée de `main` à la fusion (index corrigé) → §2 quater |
| 23 | tests en double | ⚠️ conservés (règle : ne pas supprimer de tests) |
| 24 | smoke test PowerShell | ⚠️ conservé ; `run_local_demo.sh` ajouté pour Linux/macOS |

**Nouveaux défauts trouvés et corrigés en V1** (absents de l'audit) :
- `rate(x[30d])` sous-estime les séries jeunes ;
- séries cAdvisor du cgroup pod comptées deux fois ;
- recommandations dupliquées à chaque cycle ;
- recommandations résolues jamais clôturées ;
- workloads qui disparaissent du plan de failover quand un cluster tombe (2 variantes).
