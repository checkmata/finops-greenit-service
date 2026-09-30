"""
Banking workload — synthetic transaction processor (NO real banking data).

Purpose: generate a realistic, CPU/memory-bearing load on Kubernetes so the
FinOps/Green IT service has something real to measure. Business logic is a
deliberately simplified imitation of an AML (anti-money-laundering) pipeline:

    generator  → synthetic accounts + transfers (log-normal amounts,
                 small share of AML-like patterns: fan-out, cycles, structuring)
    processor  → per-transaction scoring (rules + SHA-256 "signature" work)
    metrics    → /metrics (Prometheus): transactions, flagged, latency

Why not AMLSim directly: IBM AMLSim (Apache-2.0) is a Java batch generator that
writes CSV datasets; it does not serve a live load. Its typologies inspired the
patterns below. See docs/WORKLOADS.md for the comparison and licences.

Stdlib + prometheus_client only. Deterministic with SEED for tests.
"""

from __future__ import annotations

import hashlib
import os
import random
import threading
import time
from dataclasses import dataclass

from prometheus_client import Counter, Histogram, start_http_server

TX_TOTAL = Counter("banking_transactions_total", "Synthetic transactions processed", ["channel"])
TX_FLAGGED = Counter("banking_transactions_flagged_total", "Transactions flagged by AML rules", ["rule"])
TX_LATENCY = Histogram("banking_scoring_seconds", "Scoring latency per transaction")

STRUCTURING_LIMIT = 10_000.0  # amounts just below a reporting threshold are suspicious


@dataclass(frozen=True)
class Transaction:
    tx_id: int
    src: str
    dst: str
    amount: float
    channel: str
    pattern: str  # "normal" | "fan_out" | "cycle" | "structuring" (ground truth, synthetic)


class TransactionGenerator:
    def __init__(self, accounts: int = 5000, suspicious_rate: float = 0.02, seed: int | None = None):
        self.rng = random.Random(seed)
        self.accounts = [f"ACC{i:06d}" for i in range(accounts)]
        self.suspicious_rate = suspicious_rate
        self._next = 0

    def _id(self) -> int:
        self._next += 1
        return self._next

    def batch(self, n: int) -> list[Transaction]:
        out: list[Transaction] = []
        while len(out) < n:
            if self.rng.random() < self.suspicious_rate:
                out.extend(self._suspicious())
            else:
                a, b = self.rng.sample(self.accounts, 2)
                amount = round(self.rng.lognormvariate(4.5, 1.2), 2)  # median ≈ 90
                out.append(Transaction(self._id(), a, b, amount,
                                       self.rng.choice(["card", "transfer", "mobile"]), "normal"))
        return out[:n]

    def _suspicious(self) -> list[Transaction]:
        kind = self.rng.choice(["fan_out", "cycle", "structuring"])
        src = self.rng.choice(self.accounts)
        if kind == "fan_out":
            dsts = self.rng.sample(self.accounts, 8)
            return [Transaction(self._id(), src, d, round(self.rng.uniform(900, 1100), 2), "transfer", kind) for d in dsts]
        if kind == "cycle":
            ring = self.rng.sample(self.accounts, 4)
            amt = round(self.rng.uniform(2000, 5000), 2)
            return [Transaction(self._id(), ring[i], ring[(i + 1) % 4], amt, "transfer", kind) for i in range(4)]
        return [Transaction(self._id(), src, self.rng.choice(self.accounts),
                            round(self.rng.uniform(9000, STRUCTURING_LIMIT - 1), 2), "transfer", kind)
                for _ in range(3)]


class Scorer:
    """Simplified rule engine. The hashing loop is the CPU cost of 'real work'."""

    def __init__(self, work_rounds: int = 200):
        self.work_rounds = work_rounds
        self.out_degree: dict[str, int] = {}

    def score(self, tx: Transaction) -> list[str]:
        digest = f"{tx.tx_id}{tx.src}{tx.dst}{tx.amount}".encode()
        for _ in range(self.work_rounds):
            digest = hashlib.sha256(digest).digest()
        rules = []
        self.out_degree[tx.src] = self.out_degree.get(tx.src, 0) + 1
        if 0.9 * STRUCTURING_LIMIT <= tx.amount < STRUCTURING_LIMIT:
            rules.append("structuring")
        if self.out_degree[tx.src] >= 8:
            rules.append("fan_out")
        if tx.amount > 50_000:
            rules.append("large_amount")
        return rules


LEDGER_WRITES = Counter("banking_ledger_writes_total", "Ledger batch writes", ["result"])


class LedgerWriter:
    """
    V2.x — persists every processed transaction in the replicated ledger (PostgreSQL).

    Writes ONLY when the local database is a primary: on the standby cluster the
    ledger is a hot standby (pg_is_in_recovery() = true) until the failover
    controller promotes it; this writer then starts writing by itself. Any error
    (database down, fenced role) is counted and retried on the next batch — scoring
    continues, the business load is never blocked by the ledger.
    """

    def __init__(self, dsn: str, origin: str, connect=None):
        self.dsn, self.origin = dsn, origin
        self._connect = connect
        self.conn = None

    def _open(self):
        if self.conn is None or self.conn.closed:
            connect = self._connect
            if connect is None:
                import psycopg  # only needed when the ledger is configured
                connect = psycopg.connect
            self.conn = connect(self.dsn, connect_timeout=3, autocommit=False)
        return self.conn

    def write(self, txs: list[Transaction]) -> str:
        try:
            conn = self._open()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery()")
                if cur.fetchone()[0]:
                    conn.rollback()
                    LEDGER_WRITES.labels("standby").inc()
                    return "standby"
                cur.executemany(
                    "INSERT INTO ledger (origin, src, dst, amount, channel, pattern) VALUES (%s, %s, %s, %s, %s, %s)",
                    [(self.origin, t.src, t.dst, t.amount, t.channel, t.pattern) for t in txs])
            conn.commit()
            LEDGER_WRITES.labels("ok").inc()
            return "ok"
        except Exception:
            LEDGER_WRITES.labels("error").inc()
            try:
                if self.conn is not None:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            return "error"


def ledger_from_env() -> LedgerWriter | None:
    host, password = os.getenv("LEDGER_HOST"), os.getenv("LEDGER_PASSWORD")
    if not host or not password:
        return None
    dsn = (f"host={host} port={os.getenv('LEDGER_PORT', '5432')} dbname={os.getenv('LEDGER_DB', 'ledger')} "
           f"user={os.getenv('LEDGER_USER', 'ledger_app')} password={password}")
    return LedgerWriter(dsn, os.getenv("CLUSTER_NAME", "unknown"))


def run(tps: float, batch: int, seed: int | None) -> None:
    gen, scorer = TransactionGenerator(seed=seed), Scorer(int(os.getenv("WORK_ROUNDS", "200")))
    ledger = ledger_from_env()
    while True:
        started = time.monotonic()
        txs = gen.batch(batch)
        for tx in txs:
            t0 = time.perf_counter()
            for rule in scorer.score(tx):
                TX_FLAGGED.labels(rule).inc()
            TX_LATENCY.observe(time.perf_counter() - t0)
            TX_TOTAL.labels(tx.channel).inc()
        if ledger is not None:
            ledger.write(txs)
        if len(scorer.out_degree) > 100_000:   # bounded memory
            scorer.out_degree.clear()
        time.sleep(max(0.0, batch / tps - (time.monotonic() - started)))


if __name__ == "__main__":
    start_http_server(int(os.getenv("METRICS_PORT", "8080")))
    seed = int(os.environ["SEED"]) if os.getenv("SEED") else None
    threading.Thread(target=run, args=(float(os.getenv("TPS", "200")), int(os.getenv("BATCH", "50")), seed),
                     daemon=False).start()
