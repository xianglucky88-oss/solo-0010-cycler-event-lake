import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cyclerlake import configure, ingest, query, report, sign

with TemporaryDirectory() as temp:
    db = Path(temp) / 'cycler.sqlite'
    first = json.loads(Path(__file__).with_name('initial.json').read_text(encoding='utf-8'))
    ingest(db, first, gap_ms=12)
    assert query(db, 'bench-larch')['segments'] == []
    conflict = dict(first, chunk='bad-content', seal_ms=40, samples=[
        {'ms': 0, 'rev': 2, 'current_ma': 999},
        {'ms': 10, 'rev': 2, 'current_ma': 600},
        {'ms': 10, 'rev': 1, 'current_ma': 501}])
    try:
        ingest(db, conflict, gap_ms=12)
    except ValueError as error:
        assert 'conflicting' in str(error)
    else:
        raise AssertionError('superseded revision conflict accepted')
    assert query(db, 'bench-larch')['late'] == []
    second = dict(first, chunk='card-2', seal_ms=40, samples=[
        {'ms': 40, 'rev': 1, 'current_ma': 20},
        {'ms': 10, 'rev': 2, 'current_ma': 300},
        {'ms': 0, 'rev': 2, 'current_ma': 999}])
    ingest(db, second, gap_ms=12)
    assert ingest(db, first, gap_ms=12) == 'replay'
    out = query(db, 'bench-larch')
    assert out['net_microcoulomb'] == '2000'
    assert [(s['status'], s['right_chunk'], s['right_row']) for s in out['segments']] == [
        ('integrated', 'card-2', 2), ('gap', 'card-2', 1)]
    assert out['late'][0]['ms'] == 0
    try:
        ingest(db, dict(first, chunk='bad', seal_ms=2), gap_ms=12)
    except ValueError as error:
        assert 'regressed' in str(error)
    else:
        raise AssertionError('watermark regression accepted')
    try:
        query(db, 'unknown')
    except ValueError as error:
        assert 'unknown' in str(error)
    else:
        raise AssertionError('unknown run returned facts')
    assert query(db, 'bench-larch') == out

    # --- settlement -------------------------------------------------------
    # The gap (10..40 ms) has no sampling; an outage declares the bench could
    # only have charged <= 20 mA there, and a sealed meter covers 10..60.
    configure(db, {
        'steps': [
            {'id': 'S1', 'run': 'bench-larch', 'start_ms': 0, 'end_ms': 10},
            {'id': 'S2', 'run': 'bench-larch', 'start_ms': 10, 'end_ms': 40},
        ],
        'windows': [{'id': 'W1', 'start': 10, 'end': 40, 'in': 500, 'out': 0,
                     'error': 0, 'runs': ['bench-larch']}],
        'outages': [{'id': 'O2', 'run': 'bench-larch', 'start_ms': 10, 'end_ms': 40,
                     'max_in_ma': 20, 'max_out_ma': 10}],
    })
    steps = {entry['step']: entry for entry in report(db)['steps']}
    # S1 is fully sampled: observed, and equal in every explanation.
    assert steps['S1']['status'] == 'observed'
    assert steps['S1']['observed']['in_microcoulomb'] == '2000'
    assert steps['S1']['signable']
    # S2 is meter-backed, not observed, and jointly bounded by the meter window
    # (500) and the declared capability (20 mA * 30 ms = 600).  Shows in/out
    # separately, so a zero net would not hide throughput.
    assert steps['S2']['status'] in ('meter_ready', 'meter_pending')
    assert steps['S2']['observed']['in_microcoulomb'] == '0'
    assert steps['S2']['possible']['in_microcoulomb'] == ['500', '500']
    assert steps['S2']['possible']['out_microcoulomb'] == ['0', '0']
    assert any(w['type'] in ('sealed_meter', 'missing_capability')
               for w in steps['S2']['witness'])
    # Determinism: a repeated read is identical.
    assert report(db) == report(db)
    print(json.dumps(out, sort_keys=True))
