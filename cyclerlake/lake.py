from contextlib import contextmanager
import sqlite3

from .contract import decode, integer, key
from .intervals import result, seal_intervals

SCHEMA = '''
CREATE TABLE IF NOT EXISTS run_state(id TEXT PRIMARY KEY, seal_ms INTEGER, gap_ms INTEGER);
CREATE TABLE IF NOT EXISTS chunk_receipt(run TEXT, chunk TEXT, sha TEXT NOT NULL, source_json TEXT NOT NULL, PRIMARY KEY(run,chunk));
CREATE TABLE IF NOT EXISTS sample_revision(run TEXT, ms INTEGER, rev INTEGER, ma INTEGER NOT NULL, PRIMARY KEY(run,ms,rev));
CREATE TABLE IF NOT EXISTS sample(run TEXT, ms INTEGER, rev INTEGER, ma INTEGER, chunk TEXT, source_row INTEGER, PRIMARY KEY(run,ms));
CREATE TABLE IF NOT EXISTS rejected(run TEXT, chunk TEXT, source_row INTEGER, ms INTEGER, rev INTEGER, ma INTEGER, PRIMARY KEY(run,chunk,source_row));
CREATE TABLE IF NOT EXISTS segment(run TEXT,start_ms INTEGER,end_ms INTEGER,left_chunk TEXT,left_row INTEGER,right_chunk TEXT,right_row INTEGER,q2 TEXT, PRIMARY KEY(run,end_ms));
-- Settlement configuration.  Clock maps turn test milliseconds into the
-- settlement clock the independent coulometer reads.
CREATE TABLE IF NOT EXISTS clock_map(id TEXT PRIMARY KEY, offset_num INTEGER NOT NULL, offset_den INTEGER NOT NULL,
    scale_num INTEGER NOT NULL, scale_den INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meter_window(id TEXT PRIMARY KEY, seal_ms INTEGER NOT NULL,
    start_num INTEGER NOT NULL, start_den INTEGER NOT NULL, end_num INTEGER NOT NULL, end_den INTEGER NOT NULL,
    in_num INTEGER NOT NULL, in_den INTEGER NOT NULL, out_num INTEGER NOT NULL, out_den INTEGER NOT NULL,
    error_num INTEGER NOT NULL, error_den INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meter_window_run(window TEXT, run TEXT, PRIMARY KEY(window,run));
CREATE TABLE IF NOT EXISTS work_step(id TEXT PRIMARY KEY, run TEXT NOT NULL, label TEXT,
    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outage(id TEXT PRIMARY KEY, run TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
    max_in_num INTEGER NOT NULL, max_in_den INTEGER NOT NULL, max_out_num INTEGER NOT NULL, max_out_den INTEGER NOT NULL);
-- Bills are append-only: a revision adds a row and never rewrites an old one.
CREATE TABLE IF NOT EXISTS bill(step TEXT, seq INTEGER, run TEXT, in_num INTEGER, in_den INTEGER,
    out_num INTEGER, out_den INTEGER, net_num INTEGER, net_den INTEGER, signed_seal_ms INTEGER, note TEXT,
    PRIMARY KEY(step,seq));
'''


@contextmanager
def _database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


def ingest(path, chunk, *, gap_ms):
    run, name, seal, samples, digest, raw = decode(chunk)
    integer(gap_ms, 'gap_ms', 1)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        state = c.execute('SELECT seal_ms,gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
        if state and state['gap_ms'] != gap_ms:
            raise ValueError('gap policy cannot change within a run')
        receipt = c.execute('SELECT sha FROM chunk_receipt WHERE run=? AND chunk=?', (run, name)).fetchone()
        if receipt:
            if receipt['sha'] != digest:
                raise ValueError('chunk content conflict')
            return 'replay'
        old = state['seal_ms'] if state else -1
        if seal < old:
            raise ValueError('watermark regressed')
        for row, at, rev, ma in samples:
            if at <= old:
                c.execute('INSERT INTO rejected VALUES(?,?,?,?,?,?)', (run, name, row, at, rev, ma))
                continue
            known = c.execute('SELECT ma FROM sample_revision WHERE run=? AND ms=? AND rev=?', (run, at, rev)).fetchone()
            if known and known['ma'] != ma:
                raise ValueError('conflicting sample revision')
            if known is None:
                c.execute('INSERT INTO sample_revision VALUES(?,?,?,?)', (run, at, rev, ma))
            previous = c.execute('SELECT rev FROM sample WHERE run=? AND ms=?', (run, at)).fetchone()
            if not previous or previous['rev'] < rev:
                c.execute('REPLACE INTO sample VALUES(?,?,?,?,?,?)', (run, at, rev, ma, name, row))
        seal_intervals(c, run, old, seal, gap_ms)
        c.execute('REPLACE INTO run_state VALUES(?,?,?)', (run, seal, gap_ms))
        c.execute('INSERT INTO chunk_receipt VALUES(?,?,?,?)', (run, name, digest, raw))
    return 'applied'


def query(path, run):
    with _database(path) as c:
        c.execute('BEGIN')
        return result(c, key(run))
