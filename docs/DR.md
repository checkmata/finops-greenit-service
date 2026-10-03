# DR — scénario de reprise complet, mesuré (V2.x)

> Retour : [README docs](README.md) · [FAILOVER](FAILOVER.md) · [REPLICATION](REPLICATION.md) · [GITOPS](GITOPS.md)

Scripts : `scripts/dr/dr_scenario.py` (panne → failover → isolement → RPO) puis `scripts/dr/ledger-failback.sh` (resync → bascule contrôlée → failback). Tout est **réel** : deux clusters kind, la panne est un `docker pause` du nœud primary (API server, kubelet, Prometheus, workloads **et** base de données gelés d'un coup). Réglages de démo : failover après 30 s, retour après 60 s, tick 5 s, garde de données 1 s.

## 1. Déroulé

```text
NORMAL ─ docker pause primary ─► DÉTECTION ─ hystérésis 30 s ─► FAILOVER
   (1) promotion du registre (timeline +1)   (2) scaling du standby (9 Deployments)
 ─► service vérifié (écritures banking, frontend HTTP 200)
 ─ docker unpause ─► ISOLEMENT de l'ancien registre (NOLOGIN) ─► RPO + cohérence
 ─► failback BLOQUÉ par la garde de données (« primary database is fenced »)
 ─ ledger-failback.sh ─► resync (Git) ─► suspension ─► LSN égaux ─► promotion du primary
 ─► secondary redevient standby (Git) ─► hystérésis 60 s ─► FAILBACK des workloads (exact)
```

## 2. Mesures (30/09/2026)

| Mesure | Run 1 | Run 2 | **Run 3 (référence)** |
|---|---|---|---|
| Retard de réplication juste avant la panne | 0,056 s | 0,002 s | **0,078 s** |
| Détection (panne → primary marqué en échec) | 13,1 s | 13,7 s | **13,4 s** |
| Failover exécuté | 49,1 s, ❌ **failed** | 49,8 s ✅ | **49,5 s ✅** |
| Registre promu | ✅ timeline 2 | ✅ timeline 4 | **✅ timeline 6** |
| Deployments promus / disponibles | ❌ 0 (401) | 9/9 à 84,5 s | **9/9 à 84,3 s** |
| RTO banking (1ʳᵉ écriture sur le secondary) | 61,5 s | mesure fausse (voir §3) | **53,6 s** |
| RTO e-commerce (frontend HTTP 200) | 66,1 s (déjà warm) | 87,0 s | **87,0 s** |
| Trading | non promu | 2/2 disponibles, carnet vide | **2/2 disponibles, carnet vide** |
| Isolement de l'ancien primary après son retour | ~2 s | 11,3 s | **≤ 1,7 s** |
| Écritures perdues validées **avant** la panne | **0** / 69 500 | **0** / 89 300 | **0** / 113 000 |
| Écritures acquittées par l'ancien primary puis effacées (split-brain) | 100 | 1 300 | **100** |
| Cohérence (md5 des lignes communes) | ✅ | ✅ | **✅** |
| Resync du primary (Git → base backup) | 63,8 s | 53,6 s | 109,7 s |
| Écritures indisponibles pendant la bascule contrôlée | 0,9 s | 0,9 s | 1,1 s |
| Failback des workloads | ⚠️ bug (voir §3) | ✅ 9 restaurés | **✅ 9 restaurés** |
| Cohérence après failback (toutes les lignes) | — | ✅ 104 500 | **✅ 132 400** |
| Durée totale du failback | — | 168,9 s | 244,3 s |

Preuves :
- `docs/evidence/dr-scenario-2026-09-30-run{1-FAILED-token-expired,2,3}.log` ;
- `docs/evidence/dr-failback-2026-09-30-run{1,2,3}.log`.

**RTO de référence** : 53,6 s pour banking (données comprises) et 87 s pour e-commerce. Cela se décompose en ~13 s de détection, 30 s d'hystérésis, ~6 s de promotion du registre et de scaling, puis le démarrage des pods.

## 3. Défauts trouvés par ces runs (corrigés, testés)

| # | Défaut | Correction |
|---|---|---|
| 1 | **Jeton de l'actionneur expiré (24 h) → 401 au milieu du failover** (run 1). Un incident non planifié à 09:48 avait déjà échoué pour la même raison, sans que personne ne le voie | le fichier du jeton est relu à chaque appel (rotation sans redémarrage) ; `probe()` expose `actuators: {secondary: ok}` dans `/api/failover` **avant** un incident |
| 2 | **Le failback annulait un épisode plus ancien**, déjà annulé le 28/09 (il réappliquait des réplicas périmés) | seul le failover de l'épisode en cours est annulable, et jamais deux fois (test de régression) |
| 3 | Isolement lent (11,3 s) : il attendait derrière les timeouts Prometheus → 1 300 écritures exposées | boucle de garde dédiée toutes les 1 s → ≤ 1,7 s |
| 4 | Mesures du script : « 7 workloads récupérés » (warm standby déjà en marche) ; RTO banking négatif (lignes d'un run précédent) ; « retard 52 s » qui était en fait l'âge de la dernière transaction | on ne compte que les Deployments réellement scalés ; filtre `created_at > t0` ; libellé corrigé |
| 5 | Argo CD `OutOfSync` sur le StatefulSet (valeurs par défaut de `volumeClaimTemplates`) | valeurs par défaut explicites + on ignore seulement `.status` |
| 6 | Mots de passe générés avec un saut de ligne final (l'authentification aurait échoué) | corrigé avant le premier déploiement |

## 4. Ce que ce DR n'est pas

- Les deux clusters sont sur **le même hôte** : une panne matérielle réelle les toucherait tous les deux.
- **Pas de bascule du trafic** (DNS / load balancer global) : le « service vérifié » est testé depuis l'intérieur du cluster secondary.
- **TimescaleDB du service, paniers, carnet d'ordres** : non répliqués (voir [REPLICATION](REPLICATION.md) §2).
- **Réplication asynchrone** + fenêtre de split-brain d'environ 1–2 s : RPO strictement nul non garanti.
- **Hub GitOps sur `secondary`** : la reprise fonctionne quand le *primary* tombe, pas quand c'est le hub.
- Le failback des données reste une **procédure scriptée déclenchée par un humain**, pas automatique. C'est volontaire : elle efface un volume.

## 5. Incident réel du 01/10/2026 : une panne longue a fait apparaître un défaut GitOps (corrigé)

**Déroulé constaté**

| Heure (UTC) | Événement |
|---|---|
| 01:42 | l'hôte manque de mémoire, le noyau tue des processus (`journalctl` : OOM killer) |
| 01:56:39 | **failover automatique** : registre promu (timeline 8), 9 Deployments scalés sur `secondary` |
| 01:56:45 | ancien registre du primary **isolé** (rôle applicatif `NOLOGIN`) |
| 01:59:12–22 | OCM marque le primary indisponible (taint `NoSelect`) → la PlacementDecision ne le contient plus → l'ApplicationSet **supprime les 4 Applications du primary** → Argo CD supprime en cascade leurs workloads (seuls les namespaces résistent : l'identité d'Argo CD n'a pas le droit de les supprimer). `finops-platform-secondary` est aussi supprimée puis recréée : le ServiceAccount de l'actionneur change d'identifiant, son jeton devient invalide (`unauthorized`) |
| 01/10 → 03/10 | service assuré par `secondary` ; failback **bloqué** par la garde de données (voulu) |

**Ce qui a fonctionné** : détection, hystérésis, promotion des données, isolement de l'ancien primary. **Aucune donnée perdue** : le registre de `secondary` est la copie de référence.

**Ce qui n'allait pas (défaut de conception V2.x)** : la couche GitOps traitait la panne d'un cluster comme son retrait de la flotte, et elle a effacé le site en panne. Les runs DR du 30/09 ne l'avaient pas révélé : `docker pause` ne durait que ~1 min, alors qu'OCM ne déclare un cluster injoignable qu'après ~4–5 min (mesuré : 266 s).

**Correctif** (branche `fix/gitops-preserve-on-outage`)
- `gitops/apps/workloads.yaml` : `syncPolicy.preserveResourcesOnDeletion: true` sur les deux ApplicationSets. Une Application supprimée ne supprime plus ses workloads.
- `gitops/bootstrap/ocm/placements.yaml` : tolérances pour `cluster.open-cluster-management.io/unreachable` et `…/unavailable`, **sans** `tolerationSeconds`. Un cluster en panne reste une cible, quelle que soit la durée ; le retirer est une décision Git (son label `finops.io/role`).
- Jeton de l'actionneur réémis (`scripts/k8s/make-actuator-secrets.sh secondary`, 24 h).

**Remise en service (03/10, constatée)**
- Dès l'application du correctif, le contrôleur ApplicationSet a **retiré lui-même le verrou de suppression** des Applications qu'il gère. Les 4 Applications du primary bloquées ont donc été finalisées, recréées, puis resynchronisées depuis Git, **y compris `banking-primary`**.
- Ce n'était **pas anticipé** : l'intention était de garder `banking-primary` en attente.
- Conséquence vérifiée : PostgreSQL du primary a redémarré sur son ancien volume (`data-ledger-db-0`).
  - Contenu logique **inchangé** : 2 772 200 lignes, dernière écriture le 01/10 à 01:56:44, **0 ligne écrite depuis**.
  - La copie reste **isolée** (`app_login=false`) et le failback reste **bloqué** par la garde de données.
  - Seules les écritures physiques de redémarrage de PostgreSQL ont eu lieu.

**Test de non-régression**
- Statique : `tests/unit/test_v2x_gitops_outage.py`. Il échoue sans le correctif, passe avec.
- **Réel** : `scripts/dr/long-outage-test.sh` → [evidence/long-outage-test-2026-10-03.log](evidence/long-outage-test-2026-10-03.log).
  - Primary gelé 420 s ; OCM le marque `unreachable` à t+266 s.
  - Pendant toute la panne, `primary` reste dans la décision OCM et ses 4 Applications ne bougent pas.
  - Après la panne, ses 33 objets de workloads sont **les mêmes objets** (mêmes UID) : **PASS**.
- Ce test n'a pas été rejoué **sans** le correctif : cela aurait détruit les workloads du primary une seconde fois. Le défaut est établi par l'incident du 01/10 lui-même.

**Encore ouvert** (REQUIRES USER ACTION) : le failback des données (`scripts/dr/ledger-failback.sh`). Il resynchronise le registre du primary depuis `secondary` et **efface** son ancienne copie. Volontairement non exécuté.
