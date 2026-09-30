# V3 — évaluation sur l'historique RÉEL (2026-09-30 20:04 UTC)

Source : TimescaleDB, `workload_snapshots` de 2026-09-28 22:02 à 2026-09-30 20:03 UTC (46.0 h), clusters primary + secondary, 63 workloads, pas de 5 min. Généré par `scripts/ml/evaluate.py`.

## 1. Détection d'anomalies — anomalies CONTRÔLÉES injectées dans des séries réelles

39 séries actives (cpu, mémoire, énergie, coût) ; 4 pics injectés par série (+6 écarts robustes, au moins +50 % du niveau) à des positions aléatoires (graine 42).

| Méthode | Précision | Rappel | F1 | Points signalés sur les séries NON modifiées |
|---|---|---|---|---|
| rolling_zscore | 0.99 | 0.96 | 0.97 | 350 |
| ewma | 0.95 | 0.94 | 0.94 | 1230 |
| isolation_forest | 0.85 | 0.58 | 0.69 | 204 |

## 2. Détection d'anomalies — épisodes RÉELS rapprochés du journal des opérations

Toutes les opérations réelles documentées servent de « vérité terrain » : les fenêtres [début − 5 min, fin + 10 min] de chaque fichier `docs/evidence/*.log` (déploiements, tests GitOps, runs DR, failbacks) et chaque événement de `failover_events`. Un épisode CPU détecté hors de toute fenêtre est compté comme **inexpliqué** (faux positif potentiel).

**41 épisodes CPU** sur 46 h × 63 workloads : **4 expliqués** par une opération documentée, **37 inexpliqués**.
- durée ≥ 10 min (≥ 2 points) : 27/41 ; les autres sont des pics d'un seul point de 5 min ;
- épisodes simultanés sur ≥ 3 workloads (événement de cluster ou d'hôte, pas applicatif) : 12 ;
- workload le plus récurrent parmi les inexpliqués : primary/ecommerce/currencyservice (10 épisodes : rafales périodiques propres au service).
- **La précision sur données réelles n'est pas mesurable sans étiquettes** : « inexpliqué » veut dire « non documenté », pas forcément faux positif.

| Début (UTC) | Workload | Épisode | Opération correspondante |
|---|---|---|---|
| 09-29 02:55 | primary/ecommerce/currencyservice | spike ×19, pic 0.0405 vs 0.00329 cores | **inexpliqué** |
| 09-29 07:50 | primary/ecommerce/currencyservice | spike ×10, pic 0.0235 vs 0.00318 cores | **inexpliqué** |
| 09-29 12:45 | primary/ecommerce/currencyservice | spike ×9, pic 0.0205 vs 0.00334 cores | **inexpliqué** |
| 09-29 13:00 | primary/banking/fraud-scorer | spike ×5, pic 0.037 vs 0.0265 cores | **inexpliqué** |
| 09-29 13:55 | secondary/ecommerce/currencyservice | spike ×7, pic 0.0147 vs 0.000254 cores | **inexpliqué** |
| 09-29 14:00 | primary/ecommerce/paymentservice | spike ×5, pic 0.011 vs 0.000328 cores | **inexpliqué** |
| 09-29 17:40 | primary/ecommerce/currencyservice | spike ×24, pic 0.0522 vs 0.0032 cores | **inexpliqué** |
| 09-29 19:10 | secondary/ecommerce/currencyservice | spike ×5, pic 0.0112 vs 0.000237 cores | **inexpliqué** |
| 09-29 22:35 | primary/ecommerce/currencyservice | spike ×23, pic 0.0497 vs 0.00367 cores | **inexpliqué** |
| 09-29 23:00 | primary/banking/fraud-scorer | spike ×22, pic 0.0859 vs 0.0309 cores | **inexpliqué** |
| 09-29 23:05 | primary/banking/txn-processor | spike ×10, pic 0.0277 vs 0.00804 cores | **inexpliqué** |
| 09-29 23:05 | primary/ecommerce/frontend | spike ×6, pic 0.0173 vs 0.00588 cores | **inexpliqué** |
| 09-29 23:05 | primary/ecommerce/currencyservice | spike ×6, pic 0.0167 vs 0.00394 cores | **inexpliqué** |
| 09-29 23:20 | primary/ecommerce/frontend | spike ×7, pic 0.0201 vs 0.00608 cores | **inexpliqué** |
| 09-29 23:20 | primary/ecommerce/cartservice | spike ×4, pic 0.0113 vs 0.00308 cores | **inexpliqué** |
| 09-30 00:10 | secondary/ecommerce/currencyservice | spike ×8, pic 0.0171 vs 0.000942 cores | **inexpliqué** |
| 09-30 00:25 | primary/ecommerce/paymentservice | spike ×6, pic 0.0142 vs 0.00123 cores | **inexpliqué** |
| 09-30 00:35 | primary/banking/txn-processor | drop ×4, pic 0.00757 vs 0.0196 cores | **inexpliqué** |
| 09-30 00:35 | primary/ecommerce/frontend | drop ×4, pic 0.00571 vs 0.0138 cores | **inexpliqué** |
| 09-30 03:15 | primary/ecommerce/currencyservice | spike ×55, pic 0.113 vs 0.00328 cores | **inexpliqué** |
| 09-30 06:35 | primary/ecommerce/paymentservice | spike ×12, pic 0.0243 vs 0.000284 cores | **inexpliqué** |
| 09-30 06:35 | secondary/ecommerce/currencyservice | spike ×8, pic 0.0155 vs 0.000262 cores | **inexpliqué** |
| 09-30 08:05 | primary/ecommerce/currencyservice | spike ×41, pic 0.0862 vs 0.00342 cores | **inexpliqué** |
| 09-30 09:35 | primary/banking/fraud-scorer | spike ×35, pic 0.0965 vs 0.0265 cores | **inexpliqué** |
| 09-30 09:40 | primary/banking/txn-processor | spike ×10, pic 0.0272 vs 0.00669 cores | **inexpliqué** |
| 09-30 09:40 | primary/ecommerce/frontend | spike ×6, pic 0.0172 vs 0.00507 cores | **inexpliqué** |
| 09-30 09:40 | primary/ecommerce/currencyservice | spike ×12, pic 0.0269 vs 0.0033 cores | **inexpliqué** |
| 09-30 09:40 | primary/ecommerce/cartservice | spike ×23, pic 0.0487 vs 0.0027 cores | **inexpliqué** |
| 09-30 09:40 | primary/trading/market-feed | spike ×6, pic 0.0376 vs 0.0113 cores | **inexpliqué** |
| 09-30 09:40 | secondary/ecommerce/currencyservice | spike ×10, pic 0.0192 vs 0.000228 cores | **inexpliqué** |
| 09-30 09:45 | primary/ecommerce/recommendationservice | spike ×8, pic 0.0194 vs 0.00293 cores | failover_events 09-30 09:48 (failed) |
| 09-30 09:45 | primary/ecommerce/paymentservice | spike ×10, pic 0.0195 vs 0.00025 cores | failover_events 09-30 09:48 (failed) |
| 09-30 09:45 | primary/ecommerce/emailservice | spike ×13, pic 0.0264 vs 0.00135 cores | failover_events 09-30 09:48 (failed) |
| 09-30 10:00 | secondary/ecommerce/cartservice | spike ×15, pic 0.0296 vs 0.000612 cores | **inexpliqué** |
| 09-30 10:00 | secondary/ecommerce/currencyservice | spike ×8, pic 0.0159 vs 0.000254 cores | **inexpliqué** |
| 09-30 10:25 | secondary/ecommerce/currencyservice | spike ×5, pic 0.0156 vs 0.00193 cores | **inexpliqué** |
| 09-30 14:45 | primary/ecommerce/currencyservice | spike ×28, pic 0.059 vs 0.00356 cores | **inexpliqué** |
| 09-30 15:15 | primary/ecommerce/paymentservice | spike ×13, pic 0.0265 vs 0.000334 cores | **inexpliqué** |
| 09-30 15:30 | secondary/ecommerce/currencyservice | spike ×11, pic 0.0217 vs 0.000231 cores | **inexpliqué** |
| 09-30 19:10 | primary/banking/fraud-scorer | spike ×5, pic 0.0366 vs 0.026 cores | **inexpliqué** |
| 09-30 19:30 | primary/ecommerce/currencyservice | spike ×32, pic 0.0679 vs 0.0037 cores | dr-failback-2026-09-30-run1.log |

## 3. Prévision — backtest temporel (horizon 1 h = 12 × 5 min)

38 séries (5 métriques × workloads actifs avec ≥ 24 h). Paramètres et modèle choisis sur 6 origines de VALIDATION ; erreurs mesurées sur les 6 origines de TEST suivantes (jamais vues). Erreur relative = MAE / niveau moyen.

| Modèle | MAE relative médiane (test) | Meilleur sur le test (nb de séries) | Choisi sur la validation |
|---|---|---|---|
| naive | 0.101 | 8 | 10 |
| moving_average_12 | 0.060 | 8 | 6 |
| ses | 0.055 | 5 | 4 |
| holt_damped | 0.098 | 6 | 0 |
| ar6 | 0.077 | 11 | 18 |

| Métrique | naive | moving_average_12 | ses | holt_damped | ar6 |
|---|---|---|---|---|---|
| memory | 0.044 | 0.047 | 0.044 | 0.045 | 0.072 |
| energy | 0.117 | 0.050 | 0.055 | 0.100 | 0.054 |
| co2 | 0.113 | 0.068 | 0.067 | 0.100 | 0.066 |
| cpu | 0.243 | 0.201 | 0.241 | 0.257 | 0.177 |
| cost | 0.130 | 0.129 | 0.131 | 0.139 | 0.124 |

Le modèle choisi sur la validation est **pire que naive sur le test** pour 10/38 séries.

## 4. Right-sizing — backtest (apprentissage jusqu'à T−6 h, mesure sur les 6 dernières heures)

| Méthode | Séries | Taux médian de dépassement (6 h suivantes) | Séries avec > 5 % de dépassement |
|---|---|---|---|
| V3 (p95/p99 + prévision + marge CV) | 29 | 0.000 | 2 |
| V1 (moyenne × 1,2) | 29 | 0.000 | 12 |

Détail des séries où la V3 est dépassée plus de 5 % du temps :
- primary/banking/txn-processor memory : recommandé 0.0469, max observé 0.0641, dépassement 14%
- primary/trading/market-feed memory : recommandé 0.0469, max observé 0.0669, dépassement 67%

_Durée de l'évaluation : 18 s._
