import pytest

psycopg2 = pytest.importorskip("psycopg2")

from vishwakarma.storage import db as dbmod


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, params))


class FakePG:
    def __init__(self, name, fail_with=None):
        self.name = name
        self.closed = 0
        self.executed = []
        self.rollbacks = 0
        self.fail_with = fail_with

    def cursor(self, cursor_factory=None):
        if self.closed:
            raise psycopg2.InterfaceError("connection already closed")
        if self.fail_with:
            raise self.fail_with
        return FakeCursor(self)

    def close(self):
        self.closed = 1

    def rollback(self):
        self.rollbacks += 1
        if self.closed:
            raise psycopg2.InterfaceError("connection already closed")

    def commit(self):
        if self.closed:
            raise psycopg2.InterfaceError("connection already closed")


@pytest.fixture()
def make(monkeypatch):
    created = []

    def factory(first_fail=None):
        def connect(self):
            conn = FakePG(f"c{len(created)}", fail_with=first_fail if not created else None)
            created.append(conn)
            return conn
        monkeypatch.setattr(dbmod.PGConnection, "_connect", connect)
        return dbmod.PGConnection("dsn"), created

    return factory


def test_closed_connection_is_reopened_before_executing(make):
    pg, created = make()
    created[0].closed = 1
    cur = pg.execute("SELECT 1 WHERE a = ?", (5,))
    assert len(created) == 2
    assert created[1].executed == [("SELECT 1 WHERE a = %s", (5,))]
    assert cur.conn is created[1]


def test_interface_error_on_live_looking_connection_triggers_reconnect(make):
    pg, created = make(first_fail=psycopg2.InterfaceError("connection already closed"))
    pg.execute("SELECT 1")
    assert len(created) == 2 and created[1].executed


def test_operational_error_still_reconnects_once(make):
    pg, created = make(first_fail=psycopg2.OperationalError("server closed the connection"))
    pg.execute("SELECT 1")
    assert len(created) == 2 and created[1].executed


def test_gives_up_after_second_failure(monkeypatch):
    def connect(self):
        return FakePG("always-bad", fail_with=psycopg2.InterfaceError("connection already closed"))

    monkeypatch.setattr(dbmod.PGConnection, "_connect", connect)
    pg = dbmod.PGConnection("dsn")
    with pytest.raises(psycopg2.InterfaceError):
        pg.execute("SELECT 1")


def test_statement_error_rolls_back_without_masking_it(make):
    pg, created = make(first_fail=ValueError("bad sql"))
    with pytest.raises(ValueError):
        pg.execute("SELECT 1")
    assert created[0].rollbacks == 1 and len(created) == 1


def test_rollback_on_closed_connection_does_not_raise(make):
    pg, created = make()
    created[0].closed = 1
    pg.rollback()


def test_executescript_reopens_closed_connection(make):
    pg, created = make()
    created[0].closed = 1
    pg.executescript("CREATE TABLE x (a int)")
    assert len(created) == 2 and created[1].executed


def test_real_connect_passes_keepalive_and_timeout(monkeypatch):
    seen = {}

    class Conn:
        autocommit = True

    def fake_connect(dsn, **kw):
        seen.update(dsn=dsn, **kw)
        return Conn()

    monkeypatch.setattr(psycopg2, "connect", fake_connect)
    pg = dbmod.PGConnection.__new__(dbmod.PGConnection)
    pg._dsn = "postgresql://u@h/db"
    conn = dbmod.PGConnection._connect(pg)
    assert seen["keepalives"] == 1 and seen["connect_timeout"] == 10 and seen["keepalives_idle"] == 30
    assert conn.autocommit is False
