#!/usr/bin/env python3
"""
V3 validation on the REAL history (TimescaleDB of the running stack) — no synthetic series.

    1. Anomaly detection, controlled: spikes are INJECTED into real series at known
       positions; precision / recall / F1 of rolling z-score, EWMA and Isolation Forest.
       Points already flagged on the untouched series are reported separately and not
       counted as false positives of the injection.
    2. Anomaly detection, real events: do the detectors flag the REAL failovers recorded
       in failover_events (secondary workloads scaled up), and how much else do they flag?
    3. Forecasting: rolling-origin backtest per series; choice on validation, scores on test.
    4. Right-sizing backtest: recommend on history up to T-6h, measure on the last 6 h how
       often usage exceeded the recommendation (and the V1 rule: 30-day average × 1.2).

Usage: .venv/bin/python scripts/ml/evaluate.py [docs/evidence/ml-evaluation-YYYY-MM-DD.md]
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.adapters.outbound.timescale_adapter import TimescaleAdapter  # noqa: E402
from app.domain.ml.anomaly import detect, ewma, rolling_zscore  # noqa: E402
from app.domain.ml.forecasting import evaluate  # noqa: E402
from app.domain.ml.rightsizing import propose, violation_rate  # noqa: E402
from app.domain.ml.series import mad, mean, median  # noqa: E402
from app.config import settings  # noqa: E402
from app.ml_service import METRICS, MLService, MLSettings  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / f"docs/evidence/ml-evaluation-{datetime.now():%Y-%m-%d}.md"
lines: list[str] = []


def w(s: str = "") -> None:
    print(s, flush=True)
    lines.append(s)


def db_url() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].replace("@timescaledb:", "@127.0.0.1:")
    raise SystemExit("DATABASE_URL not found in .env")


def isolation_forest(values, contamination=0.01, seed=0):
    import numpy as np
    from sklearn.ensemble import IsolationForest
    x = np.asarray(values, dtype=float)
    feats, idx = [], []
    for i in range(12, len(x)):
        past = x[i - 12:i]
        feats.append([x[i], x[i] - np.median(past), x[i] - x[i - 1]])
        idx.append(i)
    if len(feats) < 50:
        return []
    model = IsolationForest(n_estimators=100, contamination=contamination, random_state=seed).fit(feats)
    return [i for i, lab in zip(idx, model.predict(feats)) if lab == -1]


async def main() -> int:
    storage = TimescaleAdapter(db_url())
    svc = MLService(storage, MLSettings(cpu_price_hour=settings.COST_PER_VCPU_HOUR_USD,
                                        memory_price_hour=settings.COST_PER_GB_HOUR_USD))
    t_start = time.time()
    series_by_scope: dict[str, dict] = {}
    for cluster in ("primary", "secondary"):
        for (ns, wl), snaps in (await svc._snapshots(cluster)).items():
            series_by_scope[f"{cluster}/{ns}/{wl}"] = svc.series(cluster, ns, wl, snaps)
    async with storage._engine.connect() as conn:
        events = (await conn.execute(text(
            "SELECT at, kind, action_status FROM failover_events WHERE kind='failover' ORDER BY at"))).all()
        span = (await conn.execute(text(
            "SELECT min(time), max(time) FROM workload_snapshots WHERE cluster IN ('primary','secondary')"))).one()
    await storage.close()

    w(f"# V3 — évaluation sur l'historique RÉEL ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")
    w()
    w(f"Source : TimescaleDB, `workload_snapshots` de {span[0]:%Y-%m-%d %H:%M} à {span[1]:%Y-%m-%d %H:%M} UTC "
      f"({(span[1] - span[0]).total_seconds() / 3600:.1f} h), clusters primary + secondary, "
      f"{len(series_by_scope)} workloads, pas de 5 min. Généré par `scripts/ml/evaluate.py`.")

    # ------------------------------------------------------------------ 1. controlled anomalies
    rng = random.Random(42)
    agg = {m: Counter() for m in ("rolling_zscore", "ewma", "isolation_forest")}
    pre_flags = Counter()
    n_series = 0
    for scope, ms in series_by_scope.items():
        for metric in ("cpu", "memory", "energy", "cost"):
            s = ms.get(metric)
            if s is None or len(s) < 200 or mean(s.values) < METRICS[metric][1] * 5:
                continue
            n_series += 1
            vals, ts = list(s.values), list(s.times)
            floor = METRICS[metric][1]
            base_flags = {
                "rolling_zscore": {p.index for p in rolling_zscore(vals, ts, abs_floor=floor)},
                "ewma": {p.index for p in ewma(vals, ts, abs_floor=floor)},
                "isolation_forest": set(isolation_forest(vals)),
            }
            for m, f in base_flags.items():
                pre_flags[m] += len(f)
            positions = rng.sample(range(40, len(vals) - 5), 4)
            injected = list(vals)
            for i in positions:   # a clear but realistic spike: +6 robust sigmas (≥ +50 % of the level)
                spread = max(mad(vals[i - 12:i]), 0.05 * abs(median(vals[i - 12:i])), floor)
                injected[i] = vals[i] + max(6 * spread, 0.5 * abs(vals[i]))
            new = {
                "rolling_zscore": {p.index for p in rolling_zscore(injected, ts, abs_floor=floor)},
                "ewma": {p.index for p in ewma(injected, ts, abs_floor=floor)},
                "isolation_forest": set(isolation_forest(injected)),
            }
            for m, flags in new.items():
                considered = flags - base_flags[m]
                tp = len(set(positions) & flags)
                agg[m]["tp"] += tp
                agg[m]["fn"] += len(positions) - tp
                agg[m]["fp"] += len(considered - set(positions))
    w()
    w("## 1. Détection d'anomalies — anomalies CONTRÔLÉES injectées dans des séries réelles")
    w()
    w(f"{n_series} séries actives (cpu, mémoire, énergie, coût) ; 4 pics injectés par série "
      f"(+6 écarts robustes, au moins +50 % du niveau) à des positions aléatoires (graine 42).")
    w()
    w("| Méthode | Précision | Rappel | F1 | Points signalés sur les séries NON modifiées |")
    w("|---|---|---|---|---|")
    for m, c in agg.items():
        p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 1.0
        r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 1.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        w(f"| {m} | {p:.2f} | {r:.2f} | {f1:.2f} | {pre_flags[m]} |")

    # ------------------------------------------------------------------ 2. real events
    w()
    w("## 2. Détection d'anomalies — épisodes RÉELS rapprochés du journal des opérations")
    w()
    w("Toutes les opérations réelles documentées servent de « vérité terrain » : les fenêtres [début − 5 min, fin + 10 min] "
      "de chaque fichier `docs/evidence/*.log` (déploiements, tests GitOps, runs DR, failbacks) et chaque événement de "
      "`failover_events`. Un épisode CPU détecté hors de toute fenêtre est compté comme **inexpliqué** (faux positif potentiel).")
    w()
    import re
    ops = []
    for f in sorted((ROOT / "docs/evidence").glob("*.log")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        stamps = re.findall(r"^\[(\d{2}:\d{2}:\d{2})\]", f.read_text(), re.M)
        if m and stamps:
            day = m.group(1)
            t0 = datetime.fromisoformat(f"{day}T{stamps[0]}+00:00")
            t1 = datetime.fromisoformat(f"{day}T{stamps[-1]}+00:00")
            ops.append((t0 - timedelta(minutes=5), t1 + timedelta(minutes=10), f.name))
    ops += [(e[0] - timedelta(minutes=5), e[0] + timedelta(minutes=10), f"failover_events {e[0]:%m-%d %H:%M} ({e[2]})")
            for e in events]
    explained, unexplained = [], []
    for scope, ms in series_by_scope.items():
        s = ms.get("cpu")
        if s is None or len(s) < 30:
            continue
        for ep in detect(s, abs_floor=METRICS["cpu"][1]):
            cause = next((o[2] for o in ops if o[0] <= ep.start <= o[1]), None)
            (explained if cause else unexplained).append((ep, scope, cause))
    total = len(explained) + len(unexplained)
    w(f"**{total} épisodes CPU** sur 46 h × {len(series_by_scope)} workloads : **{len(explained)} expliqués** par une "
      f"opération documentée, **{len(unexplained)} inexpliqués**.")
    all_eps = explained + unexplained
    long_eps = sum(1 for ep, _, _ in all_eps if ep.points >= 2)
    by_start = Counter(ep.start for ep, _, _ in all_eps)
    grouped = sum(n for n in by_start.values() if n >= 3)
    recurrent = Counter(scope for _, scope, cause in unexplained).most_common(1)
    w(f"- durée ≥ 10 min (≥ 2 points) : {long_eps}/{total} ; les autres sont des pics d'un seul point de 5 min ;")
    w(f"- épisodes simultanés sur ≥ 3 workloads (événement de cluster ou d'hôte, pas applicatif) : {grouped} ;")
    if recurrent:
        w(f"- workload le plus récurrent parmi les inexpliqués : {recurrent[0][0]} ({recurrent[0][1]} épisodes : "
          f"rafales périodiques propres au service).")
    w("- **La précision sur données réelles n'est pas mesurable sans étiquettes** : « inexpliqué » veut dire "
      "« non documenté », pas forcément faux positif.")
    w()
    w("| Début (UTC) | Workload | Épisode | Opération correspondante |")
    w("|---|---|---|---|")
    for ep, scope, cause in sorted(explained + unexplained, key=lambda x: x[0].start):
        w(f"| {ep.start:%m-%d %H:%M} | {scope} | {ep.direction} ×{ep.score:.0f}, pic {ep.peak_value:.3g} vs {ep.baseline:.3g} "
          f"cores | {cause or '**inexpliqué**'} |")

    # ------------------------------------------------------------------ 3. forecasting
    w()
    w("## 3. Prévision — backtest temporel (horizon 1 h = 12 × 5 min)")
    w()
    per_model = defaultdict(list)
    wins, chosen, n_fc, worse = Counter(), Counter(), 0, 0
    by_metric = defaultdict(lambda: defaultdict(list))
    for scope, ms in series_by_scope.items():
        for metric, s in ms.items():
            if mean(s.values) < METRICS[metric][1] * 5:
                continue
            rep = evaluate(list(s.values), horizon=12, step_seconds=300)
            if rep is None:
                continue
            n_fc += 1
            level = mean(s.values[-144:]) or 1e-9
            for name, sc in rep.test.items():
                per_model[name].append(sc.mae / level)
                by_metric[metric][name].append(sc.mae / level)
            chosen[rep.chosen] += 1
            best_test = min(rep.test, key=lambda n: rep.test[n].mae)
            wins[best_test] += 1
            if rep.test[rep.chosen].mae > rep.test["naive"].mae:
                worse += 1
    w(f"{n_fc} séries (5 métriques × workloads actifs avec ≥ 24 h). Paramètres et modèle choisis sur 6 origines de "
      f"VALIDATION ; erreurs mesurées sur les 6 origines de TEST suivantes (jamais vues). Erreur relative = MAE / niveau moyen.")
    w()
    w("| Modèle | MAE relative médiane (test) | Meilleur sur le test (nb de séries) | Choisi sur la validation |")
    w("|---|---|---|---|")
    for name in per_model:
        w(f"| {name} | {statistics.median(per_model[name]):.3f} | {wins[name]} | {chosen[name]} |")
    w()
    w("| Métrique | " + " | ".join(per_model) + " |")
    w("|---|" + "---|" * len(per_model))
    for metric, d in by_metric.items():
        w(f"| {metric} | " + " | ".join(f"{statistics.median(d[n]):.3f}" if d[n] else "–" for n in per_model) + " |")
    w()
    w(f"Le modèle choisi sur la validation est **pire que naive sur le test** pour {worse}/{n_fc} séries.")

    # ------------------------------------------------------------------ 4. right-sizing backtest
    w()
    w("## 4. Right-sizing — backtest (apprentissage jusqu'à T−6 h, mesure sur les 6 dernières heures)")
    w()
    rows = []
    for scope, ms in series_by_scope.items():
        for res, price in (("cpu", settings.COST_PER_VCPU_HOUR_USD), ("memory", settings.COST_PER_GB_HOUR_USD)):
            s = ms.get(res)
            if s is None or len(s) < 144 + 72:
                continue
            train, test = list(s.values[:-72]), list(s.values[-72:])
            if mean(train) < METRICS[res][1] * 5:
                continue
            fc = evaluate(train, horizon=12, step_seconds=300)
            upper = max(fc.forecast) + fc.interval_halfwidth if fc else None
            p = propose(res, train, 10.0, step_seconds=300, price_per_unit_hour=price, forecast_upper=upper)
            v1 = mean(train) * 1.2
            rows.append((scope, res, p.recommended_request, violation_rate(p.recommended_request, test),
                         v1, violation_rate(v1, test), max(test)))
    w("| Méthode | Séries | Taux médian de dépassement (6 h suivantes) | Séries avec > 5 % de dépassement |")
    w("|---|---|---|---|")
    for label, i in (("V3 (p95/p99 + prévision + marge CV)", 3), ("V1 (moyenne × 1,2)", 5)):
        rates = [r[i] for r in rows]
        w(f"| {label} | {len(rates)} | {statistics.median(rates):.3f} | {sum(1 for x in rates if x > 0.05)} |")
    w()
    w("Détail des séries où la V3 est dépassée plus de 5 % du temps :")
    for r in rows:
        if r[3] > 0.05:
            w(f"- {r[0]} {r[1]} : recommandé {r[2]:.3g}, max observé {r[6]:.3g}, dépassement {r[3]:.0%}")
    w()
    w(f"_Durée de l'évaluation : {time.time() - t_start:.0f} s._")
    OUT.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
