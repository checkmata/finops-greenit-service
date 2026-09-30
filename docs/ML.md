# ML — couche analytique légère et explicable (V3)

> Retour : [README docs](README.md) · Preuve : [evidence/ml-evaluation-2026-09-30.md](evidence/ml-evaluation-2026-09-30.md) · Suite : [AGENT](AGENT.md)
>
> Statut : **implémenté et validé sur l'historique réel** (46 h, 63 workloads, 2 clusters). Le ML **complète** les règles V1, il ne les remplace pas, et il ne décide jamais seul : il produit des recommandations `pending`, qu'un humain approuve.

## 1. Architecture

```text
TimescaleDB workload_snapshots (1 ligne / workload / cycle de 60 s)
   │  StoragePort.query_workload_snapshots (port existant, aucun nouvel accès)
   ▼
app/ml_service.py  ── séries régulières de 5 min (moyenne ; un trou reste un trou, jamais 0)
   │     cpu (cœurs à l'instant T) · mémoire (GiB) · coût ($/mois) · énergie (W) · CO₂ (g/h)
   ▼
app/domain/ml/   (PUR : ni numpy, ni sklearn ; testable ligne à ligne)
   ├─ anomaly.py      z-score glissant robuste (médiane/MAD) — EWMA disponible
   ├─ forecasting.py  naive · moyenne mobile · SES · Holt amorti · AR(6) + validation temporelle
   └─ rightsizing.py  p95/p99 + borne haute de prévision + marge liée au CV → requête recommandée
   ▼
Recommandations  anomaly / cost_right-sizing_ml   (boucle du LEADER, toutes les heures)
API lecture seule  GET /api/ml/anomalies · GET /api/ml/workloads/{cluster}/{ns}/{workload}
```

## 2. Donnée d'entrée : un piège trouvé sur les données réelles

Au premier essai, **0 anomalie pendant 6 failovers réels**. La cause n'était pas le modèle mais la donnée : `cpu_cores_used` est la **moyenne V1 sur toute la fenêtre d'observation** (`avg_over_time(rate[5m])[30d:5m]`). C'est juste pour le right-sizing V1, mais le signal est lissé sur des jours : `txn-processor` restait à 0,0074 ± 0,0006 cœur pendant 46 h.

Correction :
- colonne `cpu_cores_used_now` (migration 006) = CPU **à l'instant T** (taux sur 5 min, même jointure pod → workload), remplie à chaque cycle ;
- les 46 h passées ont été **re-mesurées dans le vrai Prometheus** (`scripts/ml/backfill_cpu_now.py`, 61 957 lignes). Une ligne sans échantillon à ±30 s reste NULL : rien n'est interpolé.

## 3. V3.1 — Détection d'anomalies

**Méthode retenue : z-score glissant robuste.**
- Référence = médiane des 12 points précédents (1 h).
- Dispersion = MAD × 1,4826, avec un plancher (5 % du niveau, et un minimum absolu propre à chaque métrique, p. ex. 2 millicœurs).
- Seuil = 4. Les points consécutifs sont regroupés en **épisodes**.

Chaque épisode porte : métrique, valeur (pic), référence, score, horodatage (début/fin), méthode, et une **raison** en clair (« cpu spike : pic 0,113 cores vs référence 0,0033 (+3 339 %), 55× la variabilité normale, ~10 min »).

**Anomalies contrôlées** (4 pics injectés dans chacune des 39 séries réelles actives, graine 42) :

| Méthode | Précision | Rappel | F1 | Points signalés sur les séries non modifiées |
|---|---|---|---|---|
| **z-score glissant robuste** | **1,00** | **0,99** | **0,99** | 350 |
| EWMA | 0,90 | 0,90 | 0,90 | 1 230 |
| Isolation Forest (sklearn, 100 arbres, contamination 1 %) | 0,84 | 0,58 | 0,69 | 204 |

→ **Isolation Forest n'est pas retenu** : il fait moins bien et ajouterait numpy et sklearn au runtime. Il n'est utilisé que dans `scripts/ml/evaluate.py` (dépendance de dev).

**Événements réels** (46 h) :
- 41 épisodes CPU, dont 4 correspondent à une opération documentée (runs DR, incident de 09:48) ;
- 27/41 durent au moins 10 min ;
- 12 sont simultanés sur au moins 3 workloads (événement de cluster ou d'hôte) ;
- 10 viennent de rafales périodiques de `currencyservice`.

**La précision sur données réelles n'est pas mesurable sans étiquettes** : « inexpliqué » veut dire « non documenté ». **PARTIAL**.

## 4. V3.2 — Prévision (horizon 1 h)

**Validation temporelle, jamais mélangée** : 6 origines de *validation* servent au choix des paramètres et du modèle, puis 6 origines de *test* suivantes, jamais vues, servent à mesurer.

38 séries réelles. Erreur relative = MAE / niveau moyen, médiane sur le test :

| Modèle | Global | CPU | Mémoire | Énergie | CO₂ | Coût |
|---|---|---|---|---|---|---|
| naive (référence) | 0,101 | 0,243 | 0,044 | 0,117 | 0,113 | 0,130 |
| moyenne mobile (12) | 0,060 | 0,201 | 0,047 | **0,050** | 0,068 | 0,129 |
| SES | **0,055** | 0,241 | 0,044 | 0,055 | 0,067 | 0,131 |
| Holt amorti | 0,098 | 0,257 | 0,045 | 0,100 | 0,100 | 0,139 |
| **AR(6)** (« ML » : régression sur les retards) | 0,077 | **0,177** | 0,072 | 0,054 | **0,066** | **0,124** |

Conclusions honnêtes :
- **Énergie et CO₂** : lisser divise l'erreur par deux par rapport à naive.
- **CPU** : AR(6) apporte −27 % d'erreur.
- **Mémoire** : naive est déjà très bon (la série est quasi constante) ; AR(6) fait **pire**.
- Le modèle choisi sur la validation est **pire que naive sur le test pour 10/38 séries** : les séries ne sont pas stationnaires (redémarrages, rollouts).

Pas de deep learning : quelques centaines de points par série, et des modèles linéaires suffisent. La **saisonnalité journalière** n'est pas modélisée : moins de deux cycles dans l'historique, elle serait invalidable.

Pourquoi CO₂ ≈ énergie : CO₂ = énergie × PUE × intensité **constante**. C'est le même signal remis à l'échelle ; les petites différences viennent des arrondis et des cycles manquants.

## 5. V3.3 — Right-sizing

```text
requête actuelle → p95 (CPU, compressible) / p99 (mémoire : un dépassement = OOM kill)
   → max(percentile, borne haute de prévision = prévision + bande à 95 %)
   → marge = 15 % + 0,5 × CV (plafond +35 %) → arrondi (10 m / 16 Mi)
   → confiance = historique × stabilité × qualité de prévision   (les trois facteurs sont renvoyés)
```

**Backtest** : apprentissage jusqu'à T−6 h, puis mesure sur les 6 h suivantes du taux de temps où la consommation **dépasse** la requête recommandée (29 séries).

| Méthode | Taux médian de dépassement | Séries dépassées plus de 5 % du temps |
|---|---|---|
| **V3** | 0,000 | **2** |
| Règle V1 (moyenne × 1,2) | 0,000 | 12 |

Les 2 échecs V3 sont des **changements de régime mémoire** : la nouvelle image banking (qui écrit dans le registre, déployée à 19:14) et la croissance de `market-feed`. Une recommandation vaut pour le régime observé ; elle est recalculée à chaque cycle.

**Standby exclu** : les workloads du cluster secondary ne sont **jamais** recommandés à la baisse sur leur propre usage (au repos). Ils doivent absorber la charge du primary au failover. Constaté sur les données réelles : la V3 proposait 0,1 → 0,01 cœur pour le `txn-processor` du standby. L'API montre le calcul avec une note, mais aucune recommandation n'est créée.

## 6. Défauts trouvés par la validation réelle (corrigés, testés)

| Défaut | Correction |
|---|---|
| CPU lissé (moyenne longue) : aucune anomalie visible | `cpu_cores_used_now` + réimport depuis Prometheus |
| Planchers absolus 5–25 × trop grands pour ces workloads (1–70 m) | planchers adaptés à l'échelle réelle |
| Le scheduler V1 fermait (`obsolete`) les recommandations ML chaque minute | chaque producteur ne ferme que **ses** catégories |
| Boucle ML : premier cycle sauté au démarrage (pas encore leader) → repoussé d'1 h | nouvel essai toutes les 15 s tant que la réplique n'est pas leader |
| Élection de leader appelée en parallèle par 3 boucles sur 1 connexion asyncpg | `asyncio.Lock` (test de concurrence) |
| Une hausse de requête enregistrée comme « économie » négative → **500** sur `/api/recommendations/history` | une hausse n'a pas d'économie (`None`), son surcoût est dans le message ; 15 lignes corrigées |
| Right-sizing à la baisse du standby | standby exclu |

## 7. Tests

- Unitaires : `tests/unit/test_v3_ml_domain.py` (14), `tests/unit/test_v3_ml_service.py` (10).
- Infrastructure réelle : `tests/infra/test_real_ml.py` (2 : historique réel → modèles ; API + recommandations persistées, standby exclu).
- Évaluation reproductible : `.venv/bin/python scripts/ml/evaluate.py` (16 s).

## 8. Limites

- Historique court (46 h) : pas de saisonnalité, et des prévisions limitées à 1 h.
- Pas d'étiquettes : la précision sur les anomalies réelles n'est pas mesurée.
- Une recommandation d'anomalie par (workload, métrique) : le dernier épisode remplace le précédent.
- Pas de corrélation entre workloads : un événement d'hôte produit N anomalies au lieu d'une. C'est à regrouper (V4 le fait lors de la priorisation).
