# ROADMAP — V1 → V4

> Retour : [README docs](README.md)

Principe : chaque version garde les calculs **déterministes** (formules FinOps/GreenIT, règles) et ajoute des capacités par-dessus. Rien ne passe en production sans validation humaine.

## V1 — 2 clusters + workloads + observabilité + FinOps/GreenIT ✅

| Livré | Détail |
|---|---|
| Multicluster (observation) | config de clusters, un adaptateur par cluster, `?cluster=`, `/api/clusters` |
| Failover (décision) | fonction pure + mémoire des workloads + déclaration opérateur ; démonstration scriptée |
| Workloads | banking (AML synthétique), trading (carnet d'ordres), e-commerce (Online Boutique) + overlays primary/secondary |
| Simulation locale | 2 clusters simulés + 2 Prometheus réels (processus ou compose) |
| Corrections FinOps/GreenIT | stockage, idle inconnu, provenance de l'énergie, économies bornées, coût historisé, CPU moyenné, historique minimal |
| Cycle de vie | pending/approved/rejected/executed/failed (+ obsolete côté système), transitions validées, déduplication |
| Exploitation | 4 dashboards, CI, secrets hors Git, `/metrics`, `/health/live`, seed réparé |

OpenTelemetry n'a **pas** été ajouté : aucun besoin de traces ni de métriques applicatives dans le calcul V1, et Prometheus couvre les métriques. À reconsidérer si l'on veut relier une latence métier (ex. `banking_scoring_seconds`) à son coût.

## V2 — Infrastructure réelle + failover actionné + historique long ✅ <a id="v2"></a>

| Élément | Statut | Détail |
|---|---|---|
| Validation sur infrastructure réelle | ✅ | Docker, 2 clusters kind, Kepler RAPL, TimescaleDB, Grafana, `pytest -m infra` ([INFRASTRUCTURE](INFRASTRUCTURE.md)) |
| Hystérésis configurable | ✅ | domaine pur, seuils défaillance / retour |
| Failover multicluster réel | ✅ partiel (voulu) | scaling du standby + fail-back exact, démontré ; pas de trafic ni de données ([FAILOVER](FAILOVER.md)) |
| Historique long | ✅ | agrégats continus horaires/journaliers (90 j / 730 j), semaine/mois dérivés ; par cluster / namespace / workload |
| CO₂ par workload / pod | ✅ | Kepler par pod ↔ workload ; mesuré / estimé / indisponible |
| Authentification Prometheus | ✅ | basic / bearer via env ou fichier ; TLS configurable ; aucun secret dans Git |
| HA | ✅ | élection de leader (verrou consultatif PostgreSQL), pas de snapshot dupliqué |
| Santé | ✅ | `/health/live`, `/health/ready`, `/metrics` (dont `is_leader`) |
| ACM / GitOps (Argo CD) | → V2.x | voir ci-dessous |

Légende des statuts :
- **VALIDATED** : exécuté et vérifié sur l'infrastructure réelle (kind, Docker), avec preuve dans `docs/evidence/` ;
- **MEASURED** : grandeur mesurée pendant une exécution réelle ;
- **DONE** : implémenté et testé (unitaires / intégration, **pas** sur l'infrastructure) ;
- **PARTIAL** : une partie seulement, dite explicitement ;
- **BLOCKED** : impossible dans cet environnement ;
- **DEFERRED** : reporté par choix ;
- **REQUIRES USER ACTION** : attend une décision ou une action humaine.

## V2.x — GitOps + réplication réelle + DR mesuré ✅ (30/09/2026)

| Élément | Statut | Preuve |
|---|---|---|
| RHACM | **BLOCKED** | exige OpenShift + souscription ; architecture compatible (OCM = amont) |
| OCM : ManagedClusters + Placement | **VALIDATED** | 2 clusters JOINED / AVAILABLE ; décision {primary, secondary} ([GITOPS](GITOPS.md)) |
| Argo CD (core) + ApplicationSet piloté par OCM | **VALIDATED** | 8/8 applications Synced / Healthy |
| Git → Kubernetes · dérive · retour arrière | **VALIDATED** | 88,7 s · 2,3 s · 87,6 s ([evidence](evidence/gitops-validation-2026-09-30.log)) |
| Réplicas du standby gérés par le failover, sans conflit avec Git | **VALIDATED** | test C1 + 2 runs DR |
| Réplication des workloads (manifests) | **VALIDATED** | banking, e-commerce, trading présents sur le standby |
| Réplication des données (registre banking) | **VALIDATED** | réplication en flux entre clusters, retard ≤ 0,08 s |
| Failover + promotion des données + isolement | **VALIDATED** | 3 runs, 0 ligne perdue avant la panne ([DR](DR.md)) |
| Failback contrôlé (resync + bascule) | **VALIDATED** | 0,9–1,1 s d'écritures indisponibles, empreintes identiques |
| RPO / RTO | **VALIDATED (mesurés)** | RPO 0 ligne (+ fenêtre de split-brain ~100 lignes) ; RTO 53,6 s banking / 87 s e-commerce |
| RPO strictement nul | **PARTIAL** | asynchrone + fenêtre de split-brain d'environ 1–2 s ([REPLICATION](REPLICATION.md) §4) |
| Politiques OCM (gouvernance) | **DEFERRED** | RAM de l'hôte |
| Monitoring (Helm) géré par Argo CD | **PARTIAL** | encore installé par `kind-up.sh` |
| Réplication de TimescaleDB, paniers, carnet d'ordres | **BLOCKED / non fait** | voir [REPLICATION](REPLICATION.md) §2 |

## V3 — ML léger et explicable ✅ (30/09/2026)

| Élément | Statut | Preuve |
|---|---|---|
| Détection d'anomalies (z-score robuste ; EWMA, Isolation Forest comparés) | **VALIDATED** (injectées) | F1 0,99 vs 0,90 vs 0,69 ([ML](ML.md)) |
| Précision sur les anomalies réelles | **PARTIAL** | pas d'étiquettes : 41 épisodes, dont 4 liés à une opération documentée |
| Prévision (naive, moyenne mobile, SES, Holt, AR(6)) + validation temporelle | **VALIDATED** | 38 séries ; SES/moyenne mobile/AR battent naive en médiane ; pire que naive sur 10/38 |
| Right-sizing (p95/p99 + prévision + CV + marge) | **VALIDATED** | 2/29 séries dépassées vs 12/29 pour la règle V1 |
| Recommandations ML persistées (leader, toutes les heures) | **VALIDATED** | 29 anomalies + 56 → 30 right-sizings ; standby exclu |
| Saisonnalité journalière | **DEFERRED** | moins de 2 cycles d'historique |

## V4 — Agent de recommandation + actions contrôlées ✅ (30/09/2026)

| Élément | Statut | Preuve |
|---|---|---|
| Explication (7 questions, données uniquement, pas de LLM) | **VALIDATED** | briefing réel ([AGENT](AGENT.md)) |
| Priorisation explicable (facteurs, poids, contributions, regroupement) | **VALIDATED** | classement réel sur 85 éléments |
| Proposition structurée + politique (échelonnement, standby, liste blanche…) | **VALIDATED** | refus sur le standby ; 0,4 → 0,2 échelonné |
| Validation humaine → Git → Argo CD → K8s → vérification → rollback | **VALIDATED** | [evidence/agent-e2e-2026-09-30.log](evidence/agent-e2e-2026-09-30.log) |
| Changement des métriques + nouvelle observation | **VALIDATED** | requested_cost 18,72 → 10,08 $/mois ; le ML propose l'étape suivante |
| Dry-run côté serveur Kubernetes | **DEFERRED (volontaire)** | exigerait une identité d'écriture sur le cluster ; dry-run hors ligne à la place |
| Rédaction par LLM | **DEFERRED (volontaire)** | déterministe d'abord ; adaptateur optionnel plus tard |
| Authentification forte des validations (SSO, 4 yeux) | **PARTIAL** | jeton partagé seulement |

## Livraison (état au 30/09/2026)

| Élément | Statut |
|---|---|
| Tests | **VALIDATED** : 491 unitaires/intégration + 40 sur l'infrastructure réelle ; couverture de `app/` 87 % ; CI rejouée avec `act` 3/3 |
| Commit / push de la branche `feature/v1-multicluster-workloads` | **DONE** (`193e251`, CI GitHub 3/3) ; PR et fusion : **REQUIRES USER ACTION** |
| Ancien mot de passe de dev (`bb6052c`, dépôt privé) | **VALIDATED inactif** (refusé par la base en service) ; retiré de `main` par la fusion ; historique conservé. Sa réutilisation éventuelle hors du projet est **REQUIRES USER ACTION** (voir [LIMITATIONS](LIMITATIONS.md) §2 quater) |
| Suppression des anciennes VM minikube (arrêtées) | **REQUIRES USER ACTION** (facultatif) |
| Alerting sur l'actionneur / la garde de données | **DEFERRED** |
| SSO / double validation pour les actions de l'agent | **DEFERRED** |

