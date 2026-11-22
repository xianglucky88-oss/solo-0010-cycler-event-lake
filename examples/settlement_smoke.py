"""End-to-end settlement scenarios.

Run with ``python3 -B examples/settlement_smoke.py``.  Each scenario uses a
fresh database under a temporary directory and asserts the settlement
semantics described in PROJECT_SPEC.md.
"""
import json
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cyclerlake import configure, ingest, report, sign
from cyclerlake.lake import _database
from cyclerlake.settlement import history, query_block


def chunk(run, name, seal, samples):
    return {'run': run, 'chunk': name, 'seal_ms': seal,
            'samples': [{'ms': ms, 'rev': 1, 'current_ma': ma} for ms, ma in samples]}


def by_step(path):
    return {entry['step']: entry for entry in report(path)['steps']}


def main():
    with TemporaryDirectory() as temp:
        root = Path(temp)

        # Observed vs meter-backed vs missing; joint window over two steps.
        db = root / 'a.sqlite'
        ingest(db, chunk('r1', 'a', 0, [(0, 0), (10, 1000)]), gap_ms=12)
        ingest(db, chunk('r1', 'b', 60, [(20, 0), (60, 100)]), gap_ms=12)
        configure(db, {
            'steps': [{'id': 'A1', 'run': 'r1', 'start_ms': 0, 'end_ms': 10},
                      {'id': 'A2', 'run': 'r1', 'start_ms': 10, 'end_ms': 20},
                      {'id': 'B1', 'run': 'r1', 'start_ms': 20, 'end_ms': 40},
                      {'id': 'B2', 'run': 'r1', 'start_ms': 40, 'end_ms': 60}],
            'outages': [{'id': 'O1', 'run': 'r1', 'start_ms': 20, 'end_ms': 60,
                         'max_in_ma': 100, 'max_out_ma': 50}],
        })
        steps = by_step(db)
        assert steps['A1']['status'] == 'observed'
        assert steps['A1']['observed']['in_microcoulomb'] == '5000'
        assert steps['B1']['status'] == 'missing'
        assert steps['B1']['possible']['in_microcoulomb'] == ['0', '2000']
        # One window over both gap steps: they must be settled together.
        configure(db, {'windows': [{'id': 'W1', 'start': 20, 'end': 60, 'in': 3000,
                                    'out': 500, 'error': 0, 'runs': ['r1']}]})
        out = report(db)
        steps = by_step(db)
        assert out['links'] == [{'members': ['B1', 'B2']}]
        assert steps['B1']['status'] == 'meter_pending' and not steps['B1']['signable']
        assert sign(db, 'B1', mode='group')['status'] == 'pending'
        # A split window makes each step exactly determined and signable.
        configure(db, {'windows': [{'id': 'W2', 'start': 20, 'end': 40, 'in': 1500,
                                    'out': 250, 'error': 0, 'runs': ['r1']}]})
        assert sign(db, 'B1', mode='group')['status'] == 'applied'
        assert history(db, 'B1')['bills'][0]['in_microcoulomb'] == '1500'

        # Net zero but charge moved.
        db = root / 'b.sqlite'
        ingest(db, chunk('r2', 'a', 10, [(0, 500), (10, -500)]), gap_ms=12)
        configure(db, {'steps': [{'id': 'N', 'run': 'r2', 'start_ms': 0, 'end_ms': 10}]})
        n = by_step(db)['N']
        assert n['observed'] == {'in_microcoulomb': '1250', 'out_microcoulomb': '1250'}
        assert sign(db, 'N')['bills'][0]['net_microcoulomb'] == '0'

        # Contradiction names the sealed sample pairs and the meter reading.
        db = root / 'c.sqlite'
        ingest(db, chunk('r3', 'a', 20, [(0, 0), (10, 1000), (20, 0)]), gap_ms=12)
        configure(db, {'steps': [{'id': 'C', 'run': 'r3', 'start_ms': 0, 'end_ms': 20}],
                       'windows': [{'id': 'WC', 'start': 0, 'end': 20, 'in': 1000,
                                    'out': 0, 'error': 0, 'runs': ['r3']}]})
        c = by_step(db)['C']
        assert c['status'] == 'contradiction'
        kinds = {item['kind'] for item in c['conflict_core']}
        assert kinds == {'sample', 'meter'}

        # Unsealed window is evidence of nothing; a covered sealed one is.
        db = root / 'd.sqlite'
        ingest(db, chunk('r4', 'a', 0, [(0, 0), (10, 0)]), gap_ms=12)
        ingest(db, chunk('r4', 'b', 25, [(20, 0), (40, 0)]), gap_ms=12)
        configure(db, {'steps': [{'id': 'U', 'run': 'r4', 'start_ms': 20, 'end_ms': 40}],
                       'windows': [{'id': 'WU', 'start': 20, 'end': 30, 'in': 100,
                                    'out': 0, 'error': 0, 'runs': ['r4']}]})
        assert by_step(db)['U']['status'] == 'unsealed'

        # Append-only: a signed meter bill is contradicted by later sampling.
        db = root / 'e.sqlite'
        ingest(db, chunk('r5', 'a', 40, [(0, 0), (20, 0), (40, 0)]), gap_ms=12)
        configure(db, {'steps': [{'id': 'P', 'run': 'r5', 'start_ms': 40, 'end_ms': 100}],
                       'windows': [{'id': 'WP', 'start': 40, 'end': 100, 'in': 6000,
                                    'out': 0, 'error': 0, 'runs': ['r5']}]})
        assert sign(db, 'P')['bills'][0]['in_microcoulomb'] == '6000'
        # Late sampling fully seals 40..100 and measures a different total.
        ingest(db, chunk('r5', 'b', 100,
                         [(50, 200), (60, 200), (70, 200), (80, 200), (90, 0), (100, 0)]),
                gap_ms=12)
        p = by_step(db)['P']
        assert p['status'] == 'conflict' and p['bill']['seq'] == 1
        assert p['bill']['verification'] == 'conflict'
        assert len(history(db, 'P')['bills']) == 1  # never rewritten
        # The runtime query still shows the measured integration.
        with _database(db) as conn:
            conn.execute('BEGIN')
            block = query_block(conn, 'r5')
        entry = [item for item in block['steps'] if item['step'] == 'P'][0]
        assert Fraction(entry['observed']['in_microcoulomb']) > 0
        assert entry['bill']['verification'] == 'conflict'

        print(json.dumps({'status': 'ok'}))


if __name__ == '__main__':
    main()
