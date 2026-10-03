"""V2.x banking ledger writer: writes only on a primary database, never blocks the load."""

from workloads.banking.app import LedgerWriter, TransactionGenerator, ledger_from_env


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.conn.sql.append(sql)

    def fetchone(self):
        return (self.conn.in_recovery,)

    def executemany(self, sql, rows):
        if self.conn.fail_insert:
            raise RuntimeError("role is not permitted to log in")  # fenced
        self.conn.rows.extend(rows)


class FakeConn:
    def __init__(self, in_recovery=False, fail_insert=False):
        self.in_recovery, self.fail_insert = in_recovery, fail_insert
        self.sql, self.rows, self.commits, self.closed = [], [], 0, False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def writer(conn):
    return LedgerWriter("dsn", "primary", connect=lambda *a, **k: conn)


def test_primary_database_receives_every_transaction_with_its_origin():
    conn = FakeConn()
    txs = TransactionGenerator(seed=1).batch(20)
    assert writer(conn).write(txs) == "ok"
    assert len(conn.rows) == 20 and conn.commits == 1
    assert {r[0] for r in conn.rows} == {"primary"}


def test_standby_database_is_never_written():
    conn = FakeConn(in_recovery=True)
    assert writer(conn).write(TransactionGenerator(seed=1).batch(5)) == "standby"
    assert conn.rows == [] and conn.commits == 0


def test_error_is_absorbed_and_connection_reopened_next_batch():
    bad = FakeConn(fail_insert=True)
    w = writer(bad)
    assert w.write(TransactionGenerator(seed=1).batch(5)) == "error"
    assert bad.closed and w.conn is None
    good = FakeConn()
    w._connect = lambda *a, **k: good
    assert w.write(TransactionGenerator(seed=2).batch(5)) == "ok" and len(good.rows) == 5


def test_ledger_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("LEDGER_HOST", raising=False)
    assert ledger_from_env() is None
    monkeypatch.setenv("LEDGER_HOST", "ledger-db")
    monkeypatch.delenv("LEDGER_PASSWORD", raising=False)
    assert ledger_from_env() is None  # the Secret is optional: no password → no ledger
