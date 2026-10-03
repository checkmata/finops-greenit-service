# AGENT — agent de recommandation et actions contrôlées (V4)

> Retour : [README docs](README.md) · Données en entrée : [ML](ML.md) · Preuve : [evidence/agent-e2e-2026-09-30.log](evidence/agent-e2e-2026-09-30.log)
>
> Statut : **implémenté et validé de bout en bout sur l'infrastructure réelle** (une action complète : validation humaine, commit, synchronisation Argo CD, vérification, changement des métriques, rollback).

## 1. Ce que c'est, et ce que ce n'est pas

- Un agent **spécialisé** FinOps / GreenIT / multicluster, **déterministe** : **aucun LLM**. Chaque phrase est calculée à partir de mesures stockées (recommandations, analyse ML, snapshots). Il ne peut donc pas affirmer un chiffre absent des données.
- Pas un chatbot. Il **explique**, **priorise** et **propose** ; il n'agit **jamais** sans validation humaine.
- **Aucune identité d'écriture Kubernetes** : il écrit dans **Git**, et Argo CD applique.

```text
Prometheus · TimescaleDB · règles V1 · ML V3
        ↓ recommandations pending + preuves mesurées
   AGENT ── Explain · Prioritize · Propose
        ↓ ActionProposal
   POLICY ── liste blanche, cluster actif uniquement, namespaces, cibles exclues,
             confiance, bornes, pas ≤ ±50 % (échelonnement), mémoire ≥ p99
        ↓ dry-run hors ligne = diff Git exact           → pending_approval | rejected_by_policy
   VALIDATION HUMAINE (en-tête X-Approval-Token ; désactivée sans jeton configuré)
        ↓ re-planification : refus si le dépôt a changé depuis le diff approuvé
   ACTIONNEUR ── commit Gitea (identité finops-agent, écriture sur ce seul dépôt)
        ↓ Argo CD (sondage 60 s) → Kubernetes
   VÉRIFICATION ── Prometheus / kube-state-metrics (délai max 300 s)
        ↓ executed │ failed → rollback automatique (commit inverse) → rolled_back
   AUDIT ── agent_audit : chaque étape, avec son acteur
```

## 2. Explain

Pour chaque recommandation, les 7 questions du cahier des charges, à partir des seules données :

| Question | Source |
|---|---|
| Pourquoi ? | raison produite par le moteur V3 (percentiles, prévision, variabilité) |
| Quelles métriques ? | métrique, unité, p95/p99, requête actuelle |
| Quelle évolution ? | moyenne de la 1ʳᵉ moitié vs 2ᵉ moitié de la fenêtre (stable / hausse / baisse, %) |
| Impact coût ? | économie mensuelle (réservation libérée) ou **surcoût** d'une hausse |
| Impact énergie ? | **aucun direct** pour un redimensionnement (une requête est une réservation, la consommation suit l'usage réel) ; pour une anomalie : énergie mesurée au-dessus de la référence (Kepler) |
| Impact CO₂ ? | idem × facteur (PUE × intensité constante) |
| Confiance ? | produit **décomposé** : historique × stabilité × qualité de prévision |

Exemple réel (30/09) : « p95 0,0179 cœur, requête 0,8 → ≈ 16,63 $/mois de réservation libérée ; énergie : aucune directe (0,0905 W consommés) ; confiance 0,66 = 1,0 × 0,77 × 0,86 ».

## 3. Prioritize

Score = 100 × (Σ wᵢ·fᵢ − 0,20 × risque), chaque facteur fᵢ ∈ [0, 1]. Les **facteurs, poids et contributions** sont renvoyés, avec une phrase (« tiré par impact (+0,29) et confiance (+0,13), risque −0,12 »).

| Facteur | Poids | Définition |
|---|---|---|
| impact | 0,30 | économie / plus grande économie du lot (une hausse nécessaire vaut 0,5) |
| urgency | 0,20 | hausse nécessaire 0,9 · anomalie critique 0,7 · anomalie 0,5 · baisse 0,3 · événement de cluster 1,0 |
| confidence | 0,20 | confiance ML |
| cost | 0,10 | coût mensuel du workload / plus grand du lot |
| energy / co2 | 0,05 + 0,05 | puissance du workload / plus grande du lot |
| risk | −0,20 | baisse mémoire 0,6 > baisse CPU 0,3 > hausse 0,2 ; × 2 en production |

**Regroupement** : les anomalies qui commencent au même moment sur au moins 3 workloads d'un cluster deviennent **un seul** « événement de cluster ». C'est un problème d'hôte ou de cluster, pas N problèmes applicatifs (constaté en V3 : 12 épisodes simultanés).

## 4. Propose et politique

Action structurée, conforme au cahier des charges :

```json
{"action": "resize_workload", "target": "primary/banking/txn-processor", "resource": "cpu",
 "current": "0.4 cores", "proposed": "0.2 cores", "reason": "… [per pod: workload total 0.8 cores → 0.03 cores over 2 replicas]",
 "confidence": 0.66, "requires_approval": true, "staged": true, "final_target": "0.015 cores"}
```

- **Par pod** : les recommandations ML portent sur le **total** du workload. Constaté sur les données réelles : 0,8 cœur = 2 × 0,4. Appliquer « 0,03 » tel quel à chaque pod aurait divisé la cible par deux. L'agent divise par le nombre de réplicas (kube-state-metrics).
- **Échelonnement** : au plus −50 % par action. 0,4 → 0,015 devient une première étape 0,4 → 0,2 ; l'étape suivante sera proposée d'après les nouvelles observations.
- **Refus** : action hors liste blanche (supprimer, migrer ou déplacer des données n'est même pas représentable), cluster standby (ses réplicas appartiennent au failover), namespace non autorisé, `ledger-db`, `loadgenerator`, `_unattributed`, confiance < 0,3, hors bornes, baisse mémoire sous le p99 observé (risque d'OOM kill).
- Actions disponibles : `resize_workload` (requêtes CPU / mémoire) et `scale_workload` (réplicas).

## 5. Actions contrôlées — mécanismes

| Exigence | Implémentation |
|---|---|
| RBAC minimal | lecture : Prometheus existant ; écriture : utilisateur Gitea `finops-agent`, collaborateur **write** de ce seul dépôt (jeton `write:repository`, 403 sur l'admin) ; **aucune** identité d'écriture Kubernetes ; dangereux : rien |
| Liste blanche | `ActionPolicy` (actions, clusters actifs, namespaces, cibles exclues) |
| Dry-run | **hors ligne** : diff exact des fichiers GitOps, calculé depuis le dépôt et les valeurs en direct. Un dry-run côté serveur Kubernetes exigerait une identité d'écriture sur le cluster (il n'existe pas de verbe « dry-run seulement »), c'est donc un choix délibéré. Le rendu est testé avec le vrai `kubectl kustomize` |
| Validation humaine | obligatoire pour toute action ; jeton `X-Approval-Token` (secret hors Git) ; sans jeton configuré, les validations sont désactivées (403) |
| Journal d'audit | `agent_audit` (append-only) : proposed → approved → committed → verified / verification_failed → rolled_back, avec l'acteur |
| Timeout | vérification ≤ `AGENT_VERIFY_TIMEOUT_SECONDS` (300 s), puis échec et rollback |
| Rollback | commit inverse (chaque fichier revient à son contenu d'avant ; un fichier créé est supprimé) ; **refusé** si quelqu'un a modifié le fichier depuis ; vérifié par Prometheus |
| Idempotence | clé = (cluster, namespace, cible, ressource, valeur) ; une action ouverte identique est renvoyée au lieu d'être dupliquée (index unique partiel) ; une validation répétée est sans effet |
| Concurrence | le commit exige le SHA lu (Gitea) ; un diff différent de celui qui a été approuvé n'est **jamais** appliqué |
| Reprise après crash | au démarrage, une action restée `executing` passe `failed`, puis est annulée |

Les commits de l'agent reviennent dans le projet via `scripts/gitops/publish.sh` : sinon la publication suivante les annulerait.

## 6. Validation réelle — `scripts/e2e/agent_e2e.py` (30/09/2026)

```text
1. AVANT      requête en direct 400m/pod · snapshot : requested_cost 18,72 $/mois
2. AGENT      rang 8/85, score 40,2 (impact +0,29, confiance +0,13, risque −0,12) + 7 explications
3. PROPOSE    0,4 → 0,2 cœur/pod (échelonné ; cible finale 0,015) · politique OK · diff hors ligne
              idempotence : 2ᵉ proposition → même action
4. VALIDATION sans jeton → 403 · avec jeton → 202
5. EXÉCUTION  commit 558a9b4 (finops-agent) → Argo CD Synced sur 558a9b4 → pods (0,2, 0,2) → executed en 145 s
              audit : proposed(e2e-script) → approved(e2e-human) → committed → verified(agent-executor)
6. MÉTRIQUES  snapshot TimescaleDB suivant : requested_cost 18,72 → 10,08 $/mois
7. OBSERVATION le ML voit maintenant 0,4 cœur au total → propose l'étape suivante (0,03)
8. ROLLBACK   commit inverse 3420b6f → vérifié (0,4, 0,4) → rolled_back · publish.sh ramène l'état dans le projet
```

## 7. Défauts trouvés pendant la validation réelle (corrigés, testés)

- **Totaux du workload appliqués par pod** : l'erreur aurait divisé la cible par 2. Corrigé par la conversion par pod.
- **Échappement PromQL** : `re.escape` produisait `\-`, un échappement invalide dans une chaîne PromQL (HTTP 400). Corrigé par la validation des noms (DNS-1123), ce qui évite aussi toute injection.
- **Pseudo-workload `_unattributed`** : il provoquait un 500 sur `/briefing`. Il devient maintenant un refus de planification.
- Réglage kustomize : un patch ne peut pas l'emporter sur le transformateur `replicas:`, d'où le scale réalisé par une modification ciblée de cette ligne.

## 8. Limites

- Pas de LLM, donc les explications sont des gabarits (voulu : pas de chiffres inventés). Un adaptateur de rédaction pourrait s'y ajouter sans toucher au calcul.
- Un seul conteneur par Deployment redimensionné. Online Boutique a un conteneur par pod ; sinon, c'est refusé.
- La validation n'est protégée que par un jeton partagé : ni SSO ni double validation. En production : OAuth proxy / OpenShift OAuth.
- Aucun gain énergétique direct n'est chiffré pour un redimensionnement ; la consolidation de nœuds n'est pas modélisée.
- L'action attend la synchronisation d'Argo CD (sondage 60 s) : environ 2–3 min par action.
