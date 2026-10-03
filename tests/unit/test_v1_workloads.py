"""Business logic of the synthetic workloads (they must mean something, not just burn CPU)."""

from workloads.banking.app import STRUCTURING_LIMIT, Scorer, TransactionGenerator
from workloads.trading.app import OrderBook, OrderFlow


def test_banking_generator_is_deterministic_and_mixes_patterns():
    a = TransactionGenerator(accounts=500, suspicious_rate=0.2, seed=7).batch(2000)
    b = TransactionGenerator(accounts=500, suspicious_rate=0.2, seed=7).batch(2000)
    assert a == b
    patterns = {t.pattern for t in a}
    assert patterns == {"normal", "fan_out", "cycle", "structuring"}
    assert all(t.src != t.dst for t in a if t.pattern == "normal")


def test_banking_rules_flag_synthetic_patterns():
    gen = TransactionGenerator(accounts=500, suspicious_rate=1.0, seed=3)
    scorer = Scorer(work_rounds=1)
    flagged = {rule for tx in gen.batch(300) for rule in scorer.score(tx)}
    assert {"structuring", "fan_out"} <= flagged
    assert all(tx.amount < STRUCTURING_LIMIT for tx in gen.batch(50) if tx.pattern == "structuring")


def test_order_book_price_time_priority_and_partial_fill():
    book = OrderBook()
    assert book.submit(1, "sell", 10, 101.0) == []
    assert book.submit(2, "sell", 5, 100.0) == []
    fills = book.submit(3, "buy", 12, 101.0)
    assert fills == [(100.0, 5), (101.0, 7)]          # best price first, then partial
    assert book.asks[0].qty == 3


def test_limit_order_rests_when_not_crossing():
    book = OrderBook()
    book.submit(1, "sell", 10, 105.0)
    assert book.submit(2, "buy", 10, 100.0) == [] and len(book.bids) == 1


def test_market_order_never_rests():
    book = OrderBook()
    assert book.submit(1, "buy", 10, None) == [] and not book.bids


def test_order_flow_deterministic():
    a, b = OrderFlow(seed=1), OrderFlow(seed=1)
    assert [a.next() for _ in range(50)] == [b.next() for _ in range(50)]
