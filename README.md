# finops-greenit-service

Service FinOps et Green IT pour clusters Kubernetes/OpenShift. Pour chaque cluster, namespace et workload, il estime :
- ce qui est **réservé** et ce qui est **utilisé** (CPU, mémoire, stockage), et combien cela coûte ;
- ce qui est **gaspillé** et ce qu'un redimensionnement ferait **économiser** ;
- l'**énergie** (Kepler) et les **émissions de CO₂**, en indiquant si le chiffre est mesuré ou estimé.

Il produit des **recommandations** qu'un humain approuve ou rejette. Avec plusieurs clusters, il détecte une panne avec **hystérésis** et peut **promouvoir le cluster standby** (scaling de Deployments existants, en mode `auto` ou après approbation humaine), puis restaurer à l'identique au retour. Il ne déplace ni le trafic ni les données : ce n'est pas un plan de reprise complet.

> **V2.x · V3 · V4 (30/09/2026)** :
> - GitOps réel (OCM + Argo CD) ;
> - réplication en flux du registre banking et scénario de reprise mesuré (RPO 0 ligne avant la panne, RTO 53,6 s) ;
> - ML léger validé sur l'historique réel (anomalies, prévision, right-sizing) ;
> - agent déterministe qui explique, priorise et propose des actions **validées par un humain** puis appliquées **via Git**.
>
> Statuts détaillés : [docs/ROADMAP.md](docs/ROADMAP.md).
>
> Version **V2** : validée sur infrastructure **réelle** (Docker, 2 clusters kind, Kepler RAPL, TimescaleDB, Grafana, 2 répliques HA) — voir [docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md). Failover actionné avec hystérésis, historique heure/jour/semaine/mois, CO₂ par workload, Prometheus authentifié, élection de leader. Le ML (V3) et l'agent (V4) ne sont **pas** implémentés. Voir [docs/ROADMAP.md](docs/ROADMAP.md) et [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## Démarrage rapide

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -m "not infra" -q                   # tests unitaires + intégration, sans réseau ni base
eval "$(scripts/infra_env.sh)" && pytest -m infra   # tests sur l'infrastructure réelle
```

### Démo locale sans Docker (clusters **simulés** — démonstration, pas une validation)

```bash
# télécharger un binaire Prometheus (ex. 2.53.2) puis :
PROMETHEUS_BIN=/chemin/prometheus scripts/run_local_demo.sh
# autre terminal :
curl localhost:8080/api/clusters
curl "localhost:8080/api/finops/namespace/banking?cluster=primary"
curl "localhost:8080/api/greenit/namespace/banking?cluster=secondary"     # énergie « estimated »
scripts/simulate_failover.sh                                               # ~2 min
```

### Pile complète avec Docker Compose (TimescaleDB + Grafana)

```bash
cp .env.example .env            # choisir POSTGRES_PASSWORD (dev uniquement, .env est ignoré par git)
docker compose up -d --build
# API http://localhost:8080/docs · Grafana http://localhost:3000 · Prometheus :9090 / :9091
```

### Deux vrais clusters Kubernetes (kind)

```bash
scripts/k8s/kind-up.sh primary && scripts/k8s/kind-up.sh secondary
scripts/k8s/make-actuator-secrets.sh secondary 24h && scripts/k8s/make-prometheus-auth.sh secondary   # token court ; relu à chaud
docker compose -f docker-compose.yaml -f docker-compose.kind.yaml --profile ha up -d --build
scripts/k8s/failover-demo.sh    # panne réelle (docker pause) → failover → fail-back
```

### GitOps, reprise, ML, agent (V2.x → V4)

```bash
scripts/gitops/gitea-up.sh && scripts/gitops/ocm-up.sh     # Git local + hub OCM
# étape humaine d'octroi de droits : docs/GITOPS.md §5
scripts/gitops/publish.sh "msg" && scripts/gitops/argocd-up.sh
scripts/dr/make-ledger-secrets.sh                          # registre répliqué (secrets hors Git)
scripts/dr/gitops-validation.sh                            # Git → K8s, dérive, propriété du standby
scripts/dr/dr_scenario.py && scripts/dr/ledger-failback.sh # panne réelle, RPO/RTO, retour vers le primary
.venv/bin/python scripts/ml/evaluate.py                    # évaluation ML sur l'historique réel
scripts/gitops/make-agent-identity.sh && scripts/e2e/agent_e2e.py   # action de l'agent de bout en bout
```

## Architecture (hexagonale)

```text
app/domain/      calculs purs : finops_engine, greenit_engine, recommendation_engine, failover, history,
                 data_replication (V2.x), ml/ (V3), agent/ (V4)
app/ports/       MetricsPort, StoragePort, FailoverStatePort, FailoverActuatorPort, LeaderElectionPort,
                 DataReplicationPort (V2.x), ActionStorePort / GitOpsWriterPort / ObservationPort (V4)
app/adapters/    inbound : FastAPI · outbound : Prometheus (auth/TLS), TimescaleDB, API Kubernetes (scale), verrou PostgreSQL
app/scheduler/   cycle multicluster toutes les 5 min + boucle de failover (leader uniquement)
exporter/        simulateur de clusters (démo locale)
workloads/       banking, trading, ecommerce + overlays kustomize primary/secondary
manifests/       déploiement OpenShift du service, de TimescaleDB et de Kepler
```

## API principale

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/api/finops/namespace/{ns}?cluster=` | coûts réservé/utilisé/gaspillé par ressource et par workload |
| GET | `/api/greenit/namespace/{ns}?cluster=` | énergie, CO₂, provenance |
| GET | `/api/clusters` | santé des clusters + plan de failover |
| PUT | `/api/clusters/{name}/availability` | déclaration opérateur |
| GET | `/api/recommendations/history` | recommandations (vrais ids, statut) |
| PATCH | `/api/recommendations/{id}/status` | décision humaine (transitions validées) |
| GET | `/api/history/timeseries` | coût / énergie / CO₂ / gaspillage / économies dans le temps |
| GET | `/health`, `/health/live`, `/metrics` | exploitation |

## Documentation

[docs/README.md](docs/README.md) — guide pédagogique, architecture, flux de données, FinOps, GreenIT, multicluster, failover, workloads, limites, feuille de route, préparation d'entretien.

## Secrets

`.env` sert **uniquement** au développement local (il est ignoré par git). En Kubernetes/OpenShift, utiliser des Secrets créés hors de Git (voir `manifests/*secret.example.yaml`) ou un gestionnaire de secrets.
