"""Create immutable sample-to-sample facts in watermark order."""
from fractions import Fraction


def seal_intervals(conn, run, before, after, gap):
    prev = conn.execute('SELECT ms,rev,ma,chunk,source_row FROM sample WHERE run=? AND ms<=? ORDER BY ms DESC LIMIT 1',
                        (run, before)).fetchone()
    rows = conn.execute('SELECT ms,rev,ma,chunk,source_row FROM sample WHERE run=? AND ms>? AND ms<=? ORDER BY ms',
                        (run, before, after))
    for row in rows:
        if prev is not None:
            elapsed = row['ms'] - prev['ms']
            # Store the exact numerator over 2 as text, without SQLite integer overflow.
            q2 = str((row['ma'] + prev['ma']) * elapsed) if elapsed <= gap else None
            conn.execute('INSERT INTO segment VALUES(?,?,?,?,?,?,?,?)',
                         (run, prev['ms'], row['ms'], prev['chunk'], prev['source_row'], row['chunk'], row['source_row'], q2))
        prev = row


def result(conn, run):
    state = conn.execute('SELECT seal_ms FROM run_state WHERE id=?', (run,)).fetchone()
    if state is None:
        raise ValueError('unknown run')
    segments = []
    total = 0
    for found in conn.execute('SELECT start_ms,end_ms,left_chunk,left_row,right_chunk,right_row,q2 FROM segment WHERE run=? ORDER BY end_ms', (run,)):
        item = dict(found)
        q2 = item.pop('q2')
        total += int(q2) if q2 is not None else 0
        segments.append(dict(item, status='gap' if q2 is None else 'integrated',
                             charge_microcoulomb=None if q2 is None else str(Fraction(int(q2), 2))))
    late = [dict(row) for row in conn.execute('SELECT chunk,source_row,ms,rev,ma FROM rejected WHERE run=? ORDER BY chunk,source_row', (run,))]
    return {'seal_ms': state['seal_ms'], 'net_microcoulomb': str(Fraction(total, 2)), 'segments': segments, 'late': late}
