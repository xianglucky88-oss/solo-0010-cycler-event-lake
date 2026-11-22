"""Create immutable sample-to-sample facts in watermark order."""
from fractions import Fraction
from types import SimpleNamespace

# Sources without declared material stay selectable so single-source runs behave
# as before; declared ranks always win over this implicit fallback rank.
IMPLICIT_RANK = 10**6
MAX_MS = 10**12


def _merged(islands):
    out = []
    for lo, hi in sorted(islands):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _coverage(anchors, watermark):
    if watermark < 0:
        return []
    return _merged([[start + offset, min(end, watermark) + offset]
                    for start, end, offset in anchors if start <= min(end, watermark)])


def _covers(islands, lo, hi):
    return any(start <= lo and hi <= end for start, end in islands)


def _map(anchors, ms):
    for start, end, offset in anchors:
        if start <= ms <= end:
            return ms + offset
    return None


def _choose(ranks, availability, coverages, lo, hi):
    """Pick the source for one elementary interval, or None when undecided.

    A source must prove availability and cover the interval with its own
    anchored progress; a lower-priority source additionally needs explicit
    unavailability evidence for every higher-priority source.
    """
    best = None
    for who, rank in ranks.items():
        if not any(ok and start <= lo and hi <= end for start, end, ok in availability.get(who, ())):
            continue
        if not _covers(coverages.get(who, ()), lo, hi):
            continue
        blocked = any(orank < rank and not any(not ok and start <= lo and hi <= end
                                               for start, end, ok in availability.get(other, ()))
                      for other, orank in ranks.items())
        if blocked:
            continue
        if best is None or rank < best[0]:
            best = (rank, who)
        elif rank == best[0]:
            best = (rank, None)
    return best[1] if best else None


def _regions(ranks, availability, coverages):
    bounds = sorted({edge for table in list(availability.values()) + list(coverages.values())
                     for window in table for edge in window[:2]})
    out = []
    for lo, hi in zip(bounds, bounds[1:]):
        chosen = _choose(ranks, availability, coverages, lo, hi)
        if out and out[-1][1] == lo and out[-1][2] == chosen:
            out[-1][1] = hi
        else:
            out.append([lo, hi, chosen])
    return out


def _plan(conn, run):
    """Load one run and derive selection regions plus final canonical endpoints."""
    ranks, progress, anchors, availability = {}, {}, {}, {}
    for row in conn.execute('SELECT device,boot,rank FROM source_decl WHERE run=?', (run,)):
        ranks[(row['device'], row['boot'])] = row['rank']
    for row in conn.execute('SELECT device,boot,seal_ms FROM source_progress WHERE run=?', (run,)):
        progress[(row['device'], row['boot'])] = row['seal_ms']
    for row in conn.execute('SELECT device,boot,valid_from_ms,valid_to_ms,offset_ms FROM anchor WHERE run=?', (run,)):
        anchors.setdefault((row['device'], row['boot']), []).append(
            (row['valid_from_ms'], row['valid_to_ms'], row['offset_ms']))
    for row in conn.execute('SELECT device,boot,from_ms,to_ms,available FROM availability WHERE run=?', (run,)):
        availability.setdefault((row['device'], row['boot']), []).append(
            (row['from_ms'], row['to_ms'], bool(row['available'])))
    samples = [dict(key=(row['device'], row['boot']), ms=row['ms'], rev=row['rev'], ma=row['ma'],
                    chunk=row['chunk'], row=row['source_row'])
               for row in conn.execute('SELECT device,boot,ms,rev,ma,chunk,source_row FROM sample WHERE run=?', (run,))]
    for who in list(progress) + [s['key'] for s in samples]:
        ranks.setdefault(who, IMPLICIT_RANK)
    eff_anchors = {who: anchors.get(who, [(0, MAX_MS, 0)]) for who in ranks}
    eff_avail = {who: availability.get(who, [(0, MAX_MS, True)]) for who in ranks}
    coverages = {who: _coverage(eff_anchors[who], progress.get(who, -1)) for who in ranks}
    regs = _regions(ranks, eff_avail, coverages)
    points = []
    for sample in samples:
        if sample['ms'] > progress.get(sample['key'], -1):
            continue
        sample['canon'] = _map(eff_anchors[sample['key']], sample['ms'])
        if sample['canon'] is None:
            continue
        for idx, (lo, hi, chosen) in enumerate(regs):
            if lo <= sample['canon'] <= hi and chosen == sample['key']:
                points.append(dict(sample, region=idx))
                break
    points.sort(key=lambda s: (s['canon'], s['region']))
    seams = {(row['at_ms'], (row['left_device'], row['left_boot']), (row['right_device'], row['right_boot']))
             for row in conn.execute(
                 'SELECT at_ms,left_device,left_boot,right_device,right_boot FROM seam_evidence WHERE run=?', (run,))}
    return SimpleNamespace(ranks=ranks, progress=progress, anchors=eff_anchors,
                           availability=eff_avail, coverages=coverages, regs=regs,
                           points=points, seams=seams, samples=samples)


def _classify(regs, seams, a, b):
    """Classify the interval between consecutive endpoints.

    Returns 'normal' or 'seam-ok' when sealable, else 'undecided' or 'seam'
    with the boundary positions still missing handover evidence.
    """
    missing = []
    for idx in range(a['region'], b['region'] + 1):
        if regs[idx][2] is None:
            return 'undecided', ()
        if idx > a['region'] and regs[idx - 1][2] != regs[idx][2]:
            boundary = (regs[idx][0], regs[idx - 1][2], regs[idx][2])
            if boundary not in seams:
                missing.append(boundary)
    if missing:
        return 'seam', tuple(missing)
    crossed = any(regs[idx - 1][2] != regs[idx][2] for idx in range(a['region'] + 1, b['region'] + 1))
    return ('seam-ok' if crossed else 'normal'), ()


def seal_intervals(conn, run, gap_ms):
    """Seal every interval whose evidence is complete; never block on open seams."""
    ctx = _plan(conn, run)
    for a, b in zip(ctx.points, ctx.points[1:]):
        status, _ = _classify(ctx.regs, ctx.seams, a, b)
        if status not in ('normal', 'seam-ok'):
            continue
        found = conn.execute('SELECT 1 FROM segment WHERE run=? AND start_ms=? AND end_ms=?',
                             (run, a['canon'], b['canon'])).fetchone()
        if found:
            continue
        elapsed = b['canon'] - a['canon']
        # Store the exact numerator over 2 as text, without SQLite integer overflow.
        q2 = str((a['ma'] + b['ma']) * elapsed) if elapsed <= gap_ms else None
        conn.execute('INSERT INTO segment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (run, a['canon'], b['canon'],
                      a['key'][0], a['key'][1], a['ms'], a['chunk'], a['row'], a['ma'],
                      b['key'][0], b['key'][1], b['ms'], b['chunk'], b['row'], b['ma'],
                      'seam' if status == 'seam-ok' else 'normal', q2))


def check_consistency(conn, run):
    """Reject new evidence that would rewrite already sealed facts."""
    ctx = _plan(conn, run)
    status = {(a['canon'], b['canon']): _classify(ctx.regs, ctx.seams, a, b)[0]
              for a, b in zip(ctx.points, ctx.points[1:])}
    for seg in conn.execute('SELECT start_ms,end_ms,kind FROM segment WHERE run=?', (run,)):
        want = 'seam-ok' if seg['kind'] == 'seam' else 'normal'
        if status.get((seg['start_ms'], seg['end_ms'])) != want:
            raise ValueError('new evidence conflicts with sealed facts')


def pending_windows(conn, run):
    """Open intervals whose evidence is incomplete (breakpoints awaiting proof)."""
    ctx = _plan(conn, run)
    sealed = {(row['start_ms'], row['end_ms']) for row in
              conn.execute('SELECT start_ms,end_ms FROM segment WHERE run=?', (run,))}
    out = []
    for a, b in zip(ctx.points, ctx.points[1:]):
        if (a['canon'], b['canon']) in sealed:
            continue
        status, missing = _classify(ctx.regs, ctx.seams, a, b)
        out.append({'start_ms': a['canon'], 'end_ms': b['canon'], 'reason': status,
                    'missing_seam_ms': [at for at, _, _ in missing],
                    'left_device': a['key'][0], 'left_boot': a['key'][1],
                    'right_device': b['key'][0], 'right_boot': b['key'][1]})
    return out


def _segment_view(row):
    q2 = row['q2']
    return {'start_ms': row['start_ms'], 'end_ms': row['end_ms'],
            'left_device': row['left_device'], 'left_boot': row['left_boot'],
            'left_chunk': row['left_chunk'], 'left_row': row['left_row'],
            'right_device': row['right_device'], 'right_boot': row['right_boot'],
            'right_chunk': row['right_chunk'], 'right_row': row['right_row'],
            'kind': row['kind'], 'status': 'gap' if q2 is None else 'integrated',
            'charge_microcoulomb': None if q2 is None else str(Fraction(int(q2), 2))}


def result(conn, run):
    state = conn.execute('SELECT gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
    if state is None:
        raise ValueError('unknown run')
    ctx = _plan(conn, run)
    segments, total = [], 0
    for found in conn.execute('SELECT * FROM segment WHERE run=? ORDER BY end_ms', (run,)):
        total += int(found['q2']) if found['q2'] is not None else 0
        segments.append(_segment_view(found))
    late = [dict(row) for row in conn.execute(
        'SELECT chunk,source_row,device,boot,ms,rev,ma FROM rejected WHERE run=? ORDER BY chunk,source_row', (run,))]
    progress = [{'device': device, 'boot': boot, 'seal_ms': ctx.progress[(device, boot)],
                 'coverage': [list(window) for window in ctx.coverages[(device, boot)]]}
                for device, boot in sorted(ctx.progress)]
    selection = [{'start_ms': lo, 'end_ms': hi,
                  'device': chosen[0] if chosen else None,
                  'boot': chosen[1] if chosen else None} for lo, hi, chosen in ctx.regs]
    samples = [{'device': s['key'][0], 'boot': s['key'][1], 'ms': s['ms'], 'rev': s['rev'],
                'ma': s['ma'], 'chunk': s['chunk'], 'source_row': s['row'],
                'canonical_ms': _map(ctx.anchors[s['key']], s['ms'])}
               for s in sorted(ctx.samples, key=lambda s: (s['key'], s['ms']))]
    seal_ms = max((hi for windows in ctx.coverages.values() for _, hi in windows), default=-1)
    return {'seal_ms': seal_ms, 'net_microcoulomb': str(Fraction(total, 2)),
            'progress': progress, 'selection': selection, 'segments': segments,
            'pending': pending_windows(conn, run), 'samples': samples, 'late': late}


def _receipt_segments(conn, run, receipt):
    return conn.execute(
        'SELECT s.* FROM segment s JOIN receipt_segment rs'
        ' ON rs.run=s.run AND rs.start_ms=s.start_ms AND rs.end_ms=s.end_ms'
        ' WHERE rs.run=? AND rs.receipt=? ORDER BY s.end_ms', (run, receipt)).fetchall()


def settlement(conn, run):
    state = conn.execute('SELECT gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
    if state is None:
        raise ValueError('unknown run')
    receipts, net = [], Fraction(0)
    for rec in conn.execute('SELECT * FROM receipt WHERE run=? ORDER BY start_ms,id', (run,)):
        segs = _receipt_segments(conn, run, rec['id'])
        corrections = conn.execute(
            'SELECT id,delta_q2,note FROM correction WHERE run=? AND receipt=? ORDER BY rowid',
            (run, rec['id'])).fetchall()
        original = Fraction(int(rec['q2_total']), 2)
        current = original + sum((Fraction(int(c['delta_q2']), 2) for c in corrections), Fraction(0))
        net += current
        pending = [dict(row) for row in conn.execute(
            'SELECT start_ms,end_ms,reason FROM receipt_pending WHERE run=? AND receipt=? ORDER BY start_ms',
            (run, rec['id']))]
        receipts.append({'id': rec['id'], 'start_ms': rec['start_ms'], 'end_ms': rec['end_ms'],
                         'supplementary': any(s['kind'] == 'seam' for s in segs),
                         'original_microcoulomb': str(original), 'current_microcoulomb': str(current),
                         'corrections': [{'id': c['id'], 'delta_microcoulomb': str(Fraction(int(c['delta_q2']), 2)),
                                          'note': c['note']} for c in corrections],
                         'segments': [_segment_view(s) for s in segs],
                         'pending_windows': pending, 'note': rec['note']})
    signed = {(row['start_ms'], row['end_ms']): row['receipt'] for row in
              conn.execute('SELECT start_ms,end_ms,receipt FROM receipt_segment WHERE run=?', (run,))}
    new_coverage, new_total, unsigned = [], 0, []
    for seg in conn.execute('SELECT * FROM segment WHERE run=? ORDER BY end_ms', (run,)):
        view = _segment_view(seg)
        receipt = signed.get((seg['start_ms'], seg['end_ms']))
        if seg['kind'] == 'seam':
            new_total += int(seg['q2']) if seg['q2'] is not None else 0
            new_coverage.append({'start_ms': seg['start_ms'], 'end_ms': seg['end_ms'],
                                 'charge_microcoulomb': view['charge_microcoulomb'], 'receipt': receipt})
        if receipt is None:
            unsigned.append(view)
    return {'run': run, 'net_settled_microcoulomb': str(net), 'receipts': receipts,
            'new_coverage': new_coverage, 'new_coverage_microcoulomb': str(Fraction(new_total, 2)),
            'unsigned_segments': unsigned, 'pending_windows': pending_windows(conn, run)}
