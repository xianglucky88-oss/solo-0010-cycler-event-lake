import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cyclerlake import ingest, query

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
    print(json.dumps(out, sort_keys=True))
