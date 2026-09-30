# INTERVIEW_GUIDE — Fiche de révision orale (V1)

> Retour : [PROJECT_GUIDE](PROJECT_GUIDE.md) (les 17 questions/réponses complètes sont au [§14](PROJECT_GUIDE.md#14-questions-quun-recruteur-pourrait-poser), les pitchs aux [§15](PROJECT_GUIDE.md#15-pitch-rh--30-secondes) et [§16](PROJECT_GUIDE.md#16-explication-technique--1-à-2-minutes)).
>
> Cette fiche sert à **répéter à voix haute** avant l'entretien.

## 0. À régler AVANT l'entretien 🔴

- [ ] Mon rôle exact (MVP : commits signés « essoufi » ; V1 : réalisée avec un assistant de code — que puis-je revendiquer ?).
- [ ] Le projet a-t-il bien été réalisé chez **Eurafric Information** ? Pour quelle équipe, quel cluster ?
- [ ] A-t-il été déployé réellement ou seulement préparé ? Des résultats chiffrés réels ?
- [ ] D'où viennent les tarifs, le PUE 1,4 et les 475 gCO₂/kWh ?
- [ ] Une difficulté que **j'ai vraiment** vécue, avec ce que j'ai fait pour la résoudre.

## 1. Le projet en une phrase

> Un micro-service qui mesure, pour chaque application de plusieurs clusters Kubernetes/OpenShift, **l'écart entre ressources réservées et utilisées**. Il le traduit en **coût** et en **CO₂** (en disant si c'est mesuré ou estimé), propose des **recommandations** validées par un humain, et **décide** quel cluster devrait servir chaque application si un site tombe.

## 2. Les chiffres et formules à connaître par cœur

| Quoi | Valeur | Où |
|---|---|---|
| Coût mensuel | `quantité × tarif horaire × 720 h` (+ stockage au Go·mois) | `finops_engine.py` |
| Économie d'un right-sizing | `(request actuelle − recommandée) × prix × 720` → **Σ ≤ gaspillage** | `rightsizing_savings_usd` |
| CPU utilisé | moyenne de `rate[5m]` sur 30 j (pas `rate[30d]`) + historique minimal 24 h | `prometheus_adapter.py` |
| CO₂ | `kWh × PUE (1,4) × intensité (475) / 1000` | `greenit_engine.py` |
| Provenance énergie | conteneur / pod (mesuré) → nœud × part CPU (estimé) → indisponible | `get_energy()` |
| Seuils | < 50 % → réduire (+20 %) · > 100 % → augmenter (+25 %) · gaspillage > 70 % → CRITICAL | `recommendation_engine.py` |
| Failover | nominal / degraded / failover / outage, avec raison | `failover.py` |
| Rythme / rétention / tests | 5 min · 7 jours · 378 tests, 92 % de `app/` | |

## 3. Le schéma à savoir dessiner au tableau (30 s)

```text
 Cluster A: Kepler/cAdvisor/KSM → Prometheus A ─┐
                                                 ├─► [adaptateur/cluster] ─► [DOMAINE : FinOps · GreenIT · Règles · Failover] ─► TimescaleDB
 Cluster B: Kepler/cAdvisor/KSM → Prometheus B ─┘         ▲ API (temps réel)        ▲ Scheduler (seul écrivain)            │
                                                                                        humain (approve/reject) ◄── API/Grafana ◄┘
```

Mots-clés à placer : **hexagonale**, **provenance / qualité des données**, **décision sans actuation**, **human-in-the-loop**.

## 4. Questions techniques « pièges » et réponses honnêtes

**« Vos coûts sont-ils réels ? »**
Non, ce sont des estimations à partir de tarifs configurables, pas une facture. Elles servent à comparer et prioriser les namespaces.

**« Comment mesurez-vous l'énergie d'un conteneur ? »**
Avec Kepler, qui utilise eBPF pour attribuer la consommation aux processus. On lit son compteur de joules sur 24 h via `increase()`. Si la métrique par conteneur n'existe pas, on se rabat sur la métrique par nœud — plus grossière (tout le cluster est alors attribué au namespace).

**« Pourquoi `rate(...[30d])` ? »**
Le CPU est un compteur cumulatif de secondes ; `rate` donne la moyenne de cœurs utilisés sur 30 jours, ce qui évite de dimensionner sur un pic ponctuel. (Le champ s'appelle `cpu_core_hours` mais c'est bien un nombre moyen de cœurs.)

**« Kubernetes gère plusieurs clusters ? »**
Non. Kubernetes orchestre les workloads *dans* un cluster. La flotte relève d'ACM / Multicluster Engine / GitOps (V2). Le service observe chaque cluster et calcule une décision de failover ; il ne déplace pas le trafic.

**« Votre démo multicluster est-elle réelle ? »**
Prometheus et tout le code sont réels ; les deux clusters et leur charge sont **simulés** en local. Les vraies applications et deux clusters kind sont fournis mais n'ont pas été exécutés pendant le développement. Je le dis explicitement.

**« Que se passe-t-il si vous lancez 3 réplicas ? »**
Le scheduler tourne dans le processus de l'API : il y aurait 3 schedulers et des doublons en base. C'est pour cela que le déploiement est à 1 réplique ; l'évolution prévue est d'extraire le cycle dans un CronJob OpenShift (`run_once()` existe déjà pour ça).

**« Pourquoi ne pas écrire en base à chaque appel API ? »**
Grafana rafraîchit en boucle : on dupliquerait les lignes. Seul le scheduler écrit, les routes lisent.

**« Votre health check est-il fiable ? »**
Oui depuis V1 : `ping()` de chaque Prometheus. `/health` est `degraded` (200) si un cluster manque, `unhealthy` (503) si la base est en panne ou si aucun cluster ne répond. `/health/live` sert de liveness (ne dépend de rien d'externe).

**« Quelles limites connaissez-vous ? »**
Tarifs et facteurs carbone constants, énergie par namespace (pas par application), failover sans hystérésis, une seule réplique du service, dashboards non encore affichés. Détail : [LIMITATIONS](LIMITATIONS.md).

**« Quel bug intéressant avez-vous trouvé ? »** (seulement si tu l'as compris et vécu)
`rate(x[30d])` divise par 30 jours entiers : un workload déployé hier paraissait inactif, d'où un conseil « réduire » dangereux. Correctif : moyenne de taux sur 5 min plus un historique minimal. Découvert en faisant tourner la démo contre un vrai Prometheus, pas par les tests unitaires.

## 5. Démo possible (si on te demande de montrer)

```bash
pytest -q                                   # 378 tests en ~2 s → testabilité du domaine
PROMETHEUS_BIN=… scripts/run_local_demo.sh  # 2 clusters simulés + 2 vrais Prometheus
scripts/simulate_failover.sh                # le plan passe en failover/outage puis revient
# http://localhost:8080/docs → /api/clusters, /api/finops/namespace/banking?cluster=secondary
```

Puis ouvrir dans l'ordre : `app/domain/greenit_engine.py` (formule CO₂, lisible par tous) → `app/domain/recommendation_engine.py` (seuils) → `app/adapters/outbound/prometheus_adapter.py` (PromQL).

## 6. Vocabulaire à employer / à éviter

| Dire | Éviter |
|---|---|
| « le service **recommande** » | « le service optimise automatiquement » |
| « **estimation** du coût / du CO₂ » | « le coût réel », « la facture » |
| « cluster **OpenShift** (cloud privé) » | « AWS / Azure » (🟢 aucun cloud public dans le code) |
| « le service **décide** du failover » | « le service fait le failover » |
| « clusters **simulés** en local, kind fourni » | « déployé sur deux clusters » (🔴 non prouvé) |
| « MVP d'un mois » | « produit en production » (🔴 non prouvé) |
| mon rôle tel que confirmé | « j'ai tout développé » (🔴 non prouvé) |

## Additif V2 — questions probables

- **« Votre failover, c'est un vrai DR ? »** Non. Il détecte (avec hystérésis), il promeut le standby en scalant des Deployments existants, et il restaure exactement au retour. Démontré sur 2 clusters kind avec une panne réelle (`docker pause`). Ni le trafic ni les données ne sont déplacés ; trading n'est pas répliqué (`not_replicated`).
- **« Pourquoi une hystérésis ? »** Pour éviter le *flapping*. Pendant la démo, le nœud a réellement oscillé à sa reprise, et le chronomètre de retour a été remis à zéro.
- **« Comment évitez-vous les doublons avec 2 répliques ? »** Par un verrou consultatif PostgreSQL : un seul leader exécute le scheduler et le failover. Mesuré : aucun cycle en double, reprise en ~2,8 s.
- **« Votre énergie est-elle mesurée ? »** Sur kind, oui : Kepler lit le RAPL de l'hôte (package + DRAM). La part « other » est modélisée, donc exclue. Sur VM, elle est estimée, et affichée comme telle.
- **« Où sont les secrets ? »** Hors Git : `.env` et `.secrets/` sont ignorés ; la config ne contient que des références `*_env` / `*_file` ; les valeurs en clair sont refusées au chargement.
