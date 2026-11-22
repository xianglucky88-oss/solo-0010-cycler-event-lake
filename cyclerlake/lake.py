from contextlib import contextmanager
from fractions import Fraction
import sqlite3

from .contract import (decode, decode_anchor, decode_availability, decode_correction,
                       decode_priority, decode_seam, integer, key)
from .intervals import (check_consistency, pending_windows, result, seal_intervals, settlement)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS run_state(id TEXT PRIMARY KEY, gap_ms INTEGER);
CREATE TABLE IF NOT EXISTS source_decl(run TEXT, device TEXT, boot INTEGER, rank INTEGER NOT NULL, PRIMARY KEY(run,device,boot));
CREATE TABLE IF NOT EXISTS source_progress(run TEXT, device TEXT, boot INTEGER, seal_ms INTEGER NOT NULL, PRIMARY KEY(run,device,boot));
CREATE TABLE IF NOT EXISTS anchor(run TEXT, device TEXT, boot INTEGER, valid_from_ms INTEGER, valid_to_ms INTEGER NOT NULL, offset_ms INTEGER NOT NULL, PRIMARY KEY(run,device,boot,valid_from_ms));
CREATE TABLE IF NOT EXISTS availability(run TEXT, device TEXT, boot INTEGER, from_ms INTEGER, to_ms INTEGER NOT NULL, available INTEGER NOT NULL, PRIMARY KEY(run,device,boot,from_ms,to_ms));
CREATE TABLE IF NOT EXISTS seam_evidence(run TEXT, at_ms INTEGER, left_device TEXT, left_boot INTEGER, right_device TEXT, right_boot INTEGER, PRIMARY KEY(run,at_ms,left_device,left_boot,right_device,right_boot));
CREATE TABLE IF NOT EXISTS chunk_receipt(run TEXT, chunk TEXT, device TEXT NOT NULL, boot INTEGER NOT NULL, seal_ms INTEGER NOT NULL, sha TEXT NOT NULL, source_json TEXT NOT NULL, PRIMARY KEY(run,chunk));
CREATE TABLE IF NOT EXISTS sample_revision(run TEXT, device TEXT, boot INTEGER, ms INTEGER, rev INTEGER, ma INTEGER NOT NULL, PRIMARY KEY(run,device,boot,ms,rev));
CREATE TABLE IF NOT EXISTS sample(run TEXT, device TEXT, boot INTEGER, ms INTEGER, rev INTEGER, ma INTEGER, chunk TEXT, source_row INTEGER, PRIMARY KEY(run,device,boot,ms));
CREATE TABLE IF NOT EXISTS rejected(run TEXT, chunk TEXT, source_row INTEGER, device TEXT, boot INTEGER, ms INTEGER, rev INTEGER, ma INTEGER, PRIMARY KEY(run,chunk,source_row));
CREATE TABLE IF NOT EXISTS segment(run TEXT, start_ms INTEGER, end_ms INTEGER, left_device TEXT, left_boot INTEGER, left_ms INTEGER, left_chunk TEXT, left_row INTEGER, left_ma INTEGER, right_device TEXT, right_boot INTEGER, right_ms INTEGER, right_chunk TEXT, right_row INTEGER, right_ma INTEGER, kind TEXT NOT NULL, q2 TEXT, PRIMARY KEY(run,start_ms,end_ms));
CREATE TABLE IF NOT EXISTS receipt(run TEXT, id TEXT, start_ms INTEGER, end_ms INTEGER, q2_total TEXT NOT NULL, note TEXT, PRIMARY KEY(run,id));
CREATE TABLE IF NOT EXISTS receipt_segment(run TEXT, receipt TEXT, start_ms INTEGER, end_ms INTEGER, PRIMARY KEY(run,receipt,start_ms,end_ms));
CREATE TABLE IF NOT EXISTS receipt_pending(run TEXT, receipt TEXT, start_ms INTEGER, end_ms INTEGER, reason TEXT NOT NULL, PRIMARY KEY(run,receipt,start_ms,end_ms));
CREATE TABLE IF NOT EXISTS correction(run TEXT, id TEXT, receipt TEXT NOT NULL, note TEXT, delta_q2 TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run,id));
CREATE TABLE IF NOT EXISTS correction_item(run TEXT, correction TEXT, device TEXT, boot INTEGER, ms INTEGER, rev INTEGER, ma INTEGER NOT NULL, PRIMARY KEY(run,correction,device,boot,ms));
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


def _ensure_run(conn, run):
    conn.execute('INSERT OR IGNORE INTO run_state(id) VALUES(?)', (run,))


def _advance(conn, run):
    """Check new evidence against sealed facts, then seal whatever is provable."""
    check_consistency(conn, run)
    state = conn.execute('SELECT gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
    if state and state['gap_ms'] is not None:
        seal_intervals(conn, run, state['gap_ms'])


def ingest(path, chunk, *, gap_ms):
    run, name, device, boot, seal, samples, digest, raw = decode(chunk)
    integer(gap_ms, 'gap_ms', 1)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        state = c.execute('SELECT gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
        if state and state['gap_ms'] is not None and state['gap_ms'] != gap_ms:
            raise ValueError('gap policy cannot change within a run')
        receipt = c.execute('SELECT sha FROM chunk_receipt WHERE run=? AND chunk=?', (run, name)).fetchone()
        if receipt:
            if receipt['sha'] != digest:
                raise ValueError('chunk content conflict')
            return 'replay'
        progress = c.execute(
            'SELECT seal_ms FROM source_progress WHERE run=? AND device=? AND boot=?',
            (run, device, boot)).fetchone()
        old = progress['seal_ms'] if progress else -1
        if seal < old:
            raise ValueError('watermark regressed')
        for row, at, rev, ma in samples:
            if at <= old:
                c.execute('INSERT INTO rejected VALUES(?,?,?,?,?,?,?,?)', (run, name, row, device, boot, at, rev, ma))
                continue
            known = c.execute(
                'SELECT ma FROM sample_revision WHERE run=? AND device=? AND boot=? AND ms=? AND rev=?',
                (run, device, boot, at, rev)).fetchone()
            if known and known['ma'] != ma:
                raise ValueError('conflicting sample revision')
            if known is None:
                c.execute('INSERT INTO sample_revision VALUES(?,?,?,?,?,?)', (run, device, boot, at, rev, ma))
            previous = c.execute(
                'SELECT rev FROM sample WHERE run=? AND device=? AND boot=? AND ms=?',
                (run, device, boot, at)).fetchone()
            if not previous or previous['rev'] < rev:
                c.execute('REPLACE INTO sample VALUES(?,?,?,?,?,?,?,?)', (run, device, boot, at, rev, ma, name, row))
        c.execute('INSERT INTO run_state(id,gap_ms) VALUES(?,?)'
                  ' ON CONFLICT(id) DO UPDATE SET gap_ms=excluded.gap_ms', (run, gap_ms))
        c.execute('INSERT INTO source_progress VALUES(?,?,?,?)'
                  ' ON CONFLICT(run,device,boot) DO UPDATE SET seal_ms=excluded.seal_ms',
                  (run, device, boot, seal))
        c.execute('INSERT INTO chunk_receipt VALUES(?,?,?,?,?,?,?)', (run, name, device, boot, seal, digest, raw))
        _advance(c, run)
    return 'applied'


def declare_priority(path, material):
    run, ranks = decode_priority(material)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        _ensure_run(c, run)
        changed = False
        for device, boot, rank in ranks:
            known = c.execute(
                'SELECT rank FROM source_decl WHERE run=? AND device=? AND boot=?',
                (run, device, boot)).fetchone()
            if known:
                if known['rank'] != rank:
                    raise ValueError('source priority conflict')
                continue
            c.execute('INSERT INTO source_decl VALUES(?,?,?,?)', (run, device, boot, rank))
            changed = True
        if changed:
            _advance(c, run)
    return 'applied' if changed else 'replay'


def add_anchor(path, material):
    run, device, boot, start, end, offset = decode_anchor(material)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        _ensure_run(c, run)
        known = c.execute(
            'SELECT valid_to_ms,offset_ms FROM anchor WHERE run=? AND device=? AND boot=? AND valid_from_ms=?',
            (run, device, boot, start)).fetchone()
        if known:
            if known['valid_to_ms'] != end or known['offset_ms'] != offset:
                raise ValueError('anchor content conflict')
            return 'replay'
        for other in c.execute(
                'SELECT valid_from_ms,valid_to_ms,offset_ms FROM anchor WHERE run=? AND device=? AND boot=?',
                (run, device, boot)):
            low = min(end, other['valid_to_ms'])
            high = max(start, other['valid_from_ms'])
            if low > high:
                raise ValueError('anchor ranges conflict')
            if low == high:
                if other['offset_ms'] != offset:
                    raise ValueError('anchor ranges conflict')
            elif min(end + offset, other['valid_to_ms'] + other['offset_ms']) >= \
                    max(start + offset, other['valid_from_ms'] + other['offset_ms']):
                raise ValueError('anchor ranges conflict')
        c.execute('INSERT INTO anchor VALUES(?,?,?,?,?,?)', (run, device, boot, start, end, offset))
        _advance(c, run)
    return 'applied'


def add_availability(path, material):
    run, device, boot, start, end, available = decode_availability(material)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        _ensure_run(c, run)
        for other in c.execute(
                'SELECT from_ms,to_ms,available FROM availability WHERE run=? AND device=? AND boot=?',
                (run, device, boot)):
            if other['from_ms'] == start and other['to_ms'] == end:
                if bool(other['available']) != available:
                    raise ValueError('availability evidence conflict')
                return 'replay'
            if other['from_ms'] < end and start < other['to_ms'] and bool(other['available']) != available:
                raise ValueError('availability evidence conflict')
        c.execute('INSERT INTO availability VALUES(?,?,?,?,?,?)', (run, device, boot, start, end, int(available)))
        _advance(c, run)
    return 'applied'


def add_seam(path, material):
    run, at, left, right = decode_seam(material)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        _ensure_run(c, run)
        known = c.execute(
            'SELECT 1 FROM seam_evidence WHERE run=? AND at_ms=? AND left_device=? AND left_boot=?'
            ' AND right_device=? AND right_boot=?',
            (run, at, left[0], left[1], right[0], right[1])).fetchone()
        if known:
            return 'replay'
        c.execute('INSERT INTO seam_evidence VALUES(?,?,?,?,?,?)',
                  (run, at, left[0], left[1], right[0], right[1]))
        _advance(c, run)
    return 'applied'


def _next_receipt_id(conn, run):
    number = 1
    while conn.execute('SELECT 1 FROM receipt WHERE run=? AND id=?', (run, f'r{number}')).fetchone():
        number += 1
    return f'r{number}'


def sign(path, run, start_ms, end_ms, *, receipt_id=None, note=None):
    run = key(run)
    integer(start_ms, 'start_ms')
    integer(end_ms, 'end_ms')
    if start_ms >= end_ms:
        raise ValueError('empty sign range')
    if receipt_id is not None:
        key(receipt_id)
    if note is not None and (not isinstance(note, str) or len(note) > 200):
        raise ValueError('invalid note')
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM run_state WHERE id=?', (run,)).fetchone() is None:
            raise ValueError('unknown run')
        segments = c.execute(
            'SELECT start_ms,end_ms,q2 FROM segment WHERE run=? AND start_ms>=? AND end_ms<=? ORDER BY end_ms',
            (run, start_ms, end_ms)).fetchall()
        if not segments:
            raise ValueError('nothing settleable in range')
        for seg in segments:
            taken = c.execute(
                'SELECT receipt FROM receipt_segment WHERE run=? AND start_ms=? AND end_ms=?',
                (run, seg['start_ms'], seg['end_ms'])).fetchone()
            if taken:
                raise ValueError(f'range overlaps signed receipt {taken["receipt"]}')
        rid = receipt_id or _next_receipt_id(c, run)
        if c.execute('SELECT 1 FROM receipt WHERE run=? AND id=?', (run, rid)).fetchone():
            raise ValueError('receipt id already exists')
        total = sum(int(seg['q2']) for seg in segments if seg['q2'] is not None)
        c.execute('INSERT INTO receipt VALUES(?,?,?,?,?,?)', (run, rid, start_ms, end_ms, str(total), note))
        for seg in segments:
            c.execute('INSERT INTO receipt_segment VALUES(?,?,?,?)', (run, rid, seg['start_ms'], seg['end_ms']))
        # Keep the breakpoints and still-open proof windows on the receipt.
        for window in pending_windows(c, run):
            if window['start_ms'] < end_ms and window['end_ms'] > start_ms:
                c.execute('INSERT INTO receipt_pending VALUES(?,?,?,?,?)',
                          (run, rid, window['start_ms'], window['end_ms'], window['reason']))
    return {'status': 'signed', 'receipt': rid, 'charge_microcoulomb': str(Fraction(total, 2))}


def _effective_point(conn, run, receipt, device, boot, ms, sample_rev, sample_ma):
    rev, ma = sample_rev, sample_ma
    for row in conn.execute(
            'SELECT ci.rev,ci.ma FROM correction_item ci'
            ' JOIN correction c ON c.run=ci.run AND c.id=ci.correction'
            ' WHERE ci.run=? AND c.receipt=? AND ci.device=? AND ci.boot=? AND ci.ms=?',
            (run, receipt, device, boot, ms)):
        if row['rev'] > rev:
            rev, ma = row['rev'], row['ma']
    return rev, ma


def _effective_total(conn, run, receipt, segments):
    overrides = {}
    for row in conn.execute(
            'SELECT ci.device,ci.boot,ci.ms,ci.rev,ci.ma FROM correction_item ci'
            ' JOIN correction c ON c.run=ci.run AND c.id=ci.correction'
            ' WHERE ci.run=? AND c.receipt=?', (run, receipt)):
        point = (row['device'], row['boot'], row['ms'])
        if point not in overrides or overrides[point][0] < row['rev']:
            overrides[point] = (row['rev'], row['ma'])
    total = 0
    for seg in segments:
        if seg['q2'] is None:
            continue
        left = overrides.get((seg['left_device'], seg['left_boot'], seg['left_ms']), (None, seg['left_ma']))[1]
        right = overrides.get((seg['right_device'], seg['right_boot'], seg['right_ms']), (None, seg['right_ma']))[1]
        total += (left + right) * (seg['end_ms'] - seg['start_ms'])
    return total


def correct(path, material):
    run, cid, receipt_id, note, items, raw = decode_correction(material)
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        known = c.execute('SELECT payload,delta_q2 FROM correction WHERE run=? AND id=?', (run, cid)).fetchone()
        if known:
            if known['payload'] != raw:
                raise ValueError('correction content conflict')
            return {'status': 'replay', 'correction': cid,
                    'delta_microcoulomb': str(Fraction(int(known['delta_q2']), 2))}
        if c.execute('SELECT 1 FROM receipt WHERE run=? AND id=?', (run, receipt_id)).fetchone() is None:
            raise ValueError('unknown receipt')
        segments = c.execute(
            'SELECT s.* FROM segment s JOIN receipt_segment rs'
            ' ON rs.run=s.run AND rs.start_ms=s.start_ms AND rs.end_ms=s.end_ms'
            ' WHERE rs.run=? AND rs.receipt=?', (run, receipt_id)).fetchall()
        for device, boot, ms, rev, ma in items:
            sample = c.execute(
                'SELECT rev,ma FROM sample WHERE run=? AND device=? AND boot=? AND ms=?',
                (run, device, boot, ms)).fetchone()
            if sample is None:
                raise ValueError('correction targets unknown sample')
            covered = any((seg['left_device'], seg['left_boot'], seg['left_ms']) == (device, boot, ms) or
                          (seg['right_device'], seg['right_boot'], seg['right_ms']) == (device, boot, ms)
                          for seg in segments)
            if not covered:
                raise ValueError('correction item outside receipt coverage')
            eff_rev, eff_ma = _effective_point(c, run, receipt_id, device, boot, ms, sample['rev'], sample['ma'])
            if rev == eff_rev and ma != eff_ma:
                raise ValueError('conflicting correction')
        before = _effective_total(c, run, receipt_id, segments)
        c.execute('INSERT INTO correction VALUES(?,?,?,?,?,?)', (run, cid, receipt_id, note, '0', raw))
        for device, boot, ms, rev, ma in items:
            c.execute('INSERT INTO correction_item VALUES(?,?,?,?,?,?,?)', (run, cid, device, boot, ms, rev, ma))
        delta = _effective_total(c, run, receipt_id, segments) - before
        c.execute('UPDATE correction SET delta_q2=? WHERE run=? AND id=?', (str(delta), run, cid))
    return {'status': 'applied', 'correction': cid, 'delta_microcoulomb': str(Fraction(delta, 2))}


def query(path, run):
    with _database(path) as c:
        c.execute('BEGIN')
        return result(c, key(run))


def settle(path, run):
    with _database(path) as c:
        c.execute('BEGIN')
        return settlement(c, key(run))
