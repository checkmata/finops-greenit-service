"""
Trading workload — synthetic order flow + limit order book matching engine
(NO real market data).

    order flow   Poisson arrivals, mid-price random walk, 70 % limit / 30 % market
    matching     price-time priority limit order book (heapq), partial fills
    metrics      /metrics: orders, trades, book depth, matching latency

Why home-grown: open-source market simulators (e.g. ABIDES) are research
frameworks with heavy dependencies; this workload only needs to produce a
realistic, bursty CPU load with a business meaning. See docs/WORKLOADS.md.
"""

from __future__ import annotations

import heapq
import itertools
import math
import os
import random
import time
from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge, Histogram, start_http_server

ORDERS = Counter("trading_orders_total", "Orders received", ["side", "type"])
TRADES = Counter("trading_trades_total", "Trades executed")
VOLUME = Counter("trading_traded_quantity_total", "Quantity traded")
DEPTH = Gauge("trading_book_depth", "Resting orders", ["side"])
LATENCY = Histogram("trading_matching_seconds", "Matching latency per order")


@dataclass(order=True)
class Resting:
    sort_key: tuple
    order_id: int = field(compare=False)
    price: float = field(compare=False)
    qty: int = field(compare=False)


class OrderBook:
    """Price-time priority. Bids: max-heap (negated price). Asks: min-heap."""

    def __init__(self) -> None:
        self.bids: list[Resting] = []
        self.asks: list[Resting] = []
        self._seq = itertools.count()

    def submit(self, order_id: int, side: str, qty: int, price: float | None) -> list[tuple[float, int]]:
        """Match an incoming order; rest the remainder if it is a limit order. Returns fills."""
        fills: list[tuple[float, int]] = []
        book = self.asks if side == "buy" else self.bids
        while qty > 0 and book:
            best = book[0]
            crosses = price is None or (best.price <= price if side == "buy" else best.price >= price)
            if not crosses:
                break
            traded = min(qty, best.qty)
            fills.append((best.price, traded))
            qty -= traded
            best.qty -= traded
            if best.qty == 0:
                heapq.heappop(book)
        if qty > 0 and price is not None:
            seq = next(self._seq)
            key = (-price, seq) if side == "buy" else (price, seq)
            heapq.heappush(self.bids if side == "buy" else self.asks, Resting(key, order_id, price, qty))
        return fills


class OrderFlow:
    def __init__(self, seed: int | None = None, mid: float = 100.0):
        self.rng = random.Random(seed)
        self.mid = mid
        self._ids = itertools.count(1)

    def next(self) -> tuple[int, str, int, float | None]:
        self.mid = max(1.0, self.mid * (1 + self.rng.gauss(0, 0.0005)))
        side = self.rng.choice(["buy", "sell"])
        qty = self.rng.randint(1, 100)
        if self.rng.random() < 0.3:
            return next(self._ids), side, qty, None
        offset = abs(self.rng.gauss(0, 0.2))
        price = round(self.mid - offset if side == "buy" else self.mid + offset, 2)
        return next(self._ids), side, qty, price


def run(rate: float, seed: int | None) -> None:
    book, flow = OrderBook(), OrderFlow(seed)
    rng = random.Random(seed)
    while True:
        # Bursty arrivals: Poisson with a slowly oscillating intensity.
        intensity = rate * (1 + 0.5 * math.sin(time.time() / 600))
        time.sleep(rng.expovariate(max(intensity, 1.0)))
        oid, side, qty, price = flow.next()
        t0 = time.perf_counter()
        fills = book.submit(oid, side, qty, price)
        LATENCY.observe(time.perf_counter() - t0)
        ORDERS.labels(side, "market" if price is None else "limit").inc()
        for _, q in fills:
            TRADES.inc()
            VOLUME.inc(q)
        DEPTH.labels("bid").set(len(book.bids))
        DEPTH.labels("ask").set(len(book.asks))
        if len(book.bids) + len(book.asks) > 200_000:  # bounded memory
            book.bids, book.asks = book.bids[:50_000], book.asks[:50_000]
            heapq.heapify(book.bids)
            heapq.heapify(book.asks)


if __name__ == "__main__":
    start_http_server(int(os.getenv("METRICS_PORT", "8080")))
    run(float(os.getenv("ORDERS_PER_SECOND", "500")), int(os.environ["SEED"]) if os.getenv("SEED") else None)
