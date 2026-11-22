import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cyclerlake import (add_anchor, add_availability, add_seam, correct, declare_priority,
                        ingest, query, settle, sign)

HERE = Path(__file__).parent


def material(name, run):
    return dict(json.loads((HERE / 'handover' / name).read_text(encoding='utf-8')), run=run)


def fails(fn, fragment):
    try:
        fn()
    except ValueError as error:
        assert fragment in str(error), str(error)
    else:
        raise AssertionError(f'expected failure containing {fragment!r}')


def handover_materials(run):
    assert declare_priority(db, material('priority.json', run)) == 'applied'
    assert declare_priority(db, material('priority.json', run)) == 'replay'
    assert add_anchor(db, material('anchor-primary.json', run)) == 'applied'
    assert add_anchor(db, material('anchor-backup.json', run)) == 'applied'
    assert add_availability(db, material('availability-primary.json', run)) == 'applied'
    assert add_availability(db, material('availability-primary-down.json', run)) == 'applied'
    assert add_availability(db, material('availability-backup.json', run)) == 'applied'


def handover_chunks(run):
    assert ingest(db, material('chunk-primary-1.json', run), gap_ms=12) == 'applied'
    assert ingest(db, material('chunk-backup-1.json', run), gap_ms=12) == 'applied'
    assert ingest(db, material('chunk-backup-2.json', run), gap_ms=12) == 'applied'


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

    # --- Handover, option (a): sign the provable backup interval early --------
    handover_materials('bench-switch-a')
    handover_chunks('bench-switch-a')
    out = query(db, 'bench-switch-a')
    assert out['selection'] == [
        {'start_ms': 1000, 'end_ms': 1030, 'device': 'primary', 'boot': 1},
        {'start_ms': 1030, 'end_ms': 1100, 'device': 'backup', 'boot': 1}]
    # The open seam neither integrates nor blocks the provable backup tail.
    assert out['pending'] == [{'start_ms': 1030, 'end_ms': 1035, 'reason': 'seam',
                               'missing_seam_ms': [1030], 'left_device': 'primary', 'left_boot': 1,
                               'right_device': 'backup', 'right_boot': 1}]
    assert len(out['segments']) == 10 and out['net_microcoulomb'] == '76775/2'
    assert [s['start_ms'] for s in out['segments'] if s['right_device'] == 'backup'][0] == 1035
    assert ingest(db, material('chunk-primary-1.json', 'bench-switch-a'), gap_ms=12) == 'replay'
    assert len(query(db, 'bench-switch-a')['samples']) == 12
    fails(lambda: declare_priority(db, {'run': 'bench-switch-a', 'priorities': [
        {'device': 'primary', 'boot': 1, 'rank': 3}]}), 'priority conflict')
    fails(lambda: add_anchor(db, material('anchor-primary.json', 'bench-switch-a') | {'canonical_ms': 1001}),
          'anchor content conflict')
    fails(lambda: add_availability(db, {'run': 'bench-switch-a', 'device': 'primary', 'boot': 1,
                                        'from_ms': 1000, 'to_ms': 1030, 'available': False}),
          'availability evidence conflict')
    fails(lambda: add_availability(db, {'run': 'bench-larch', 'device': 'primary', 'boot': 1,
                                        'from_ms': 0, 'to_ms': 10, 'available': False}),
          'conflicts with sealed facts')

    assert sign(db, 'bench-switch-a', 1000, 1030) == {
        'status': 'signed', 'receipt': 'r1', 'charge_microcoulomb': '7500'}
    fails(lambda: sign(db, 'bench-switch-a', 1000, 1030), 'overlaps signed receipt r1')
    fails(lambda: sign(db, 'bench-switch-a', 2000, 3000), 'nothing settleable')
    assert sign(db, 'bench-switch-a', 1030, 1070)['receipt'] == 'r2'
    assert sign(db, 'bench-switch-a', 1070, 1100)['receipt'] == 'r3'
    view = settle(db, 'bench-switch-a')
    assert view['unsigned_segments'] == []
    assert view['pending_windows'][0]['reason'] == 'seam'
    r2 = next(r for r in view['receipts'] if r['id'] == 'r2')
    assert r2['pending_windows'] == [{'start_ms': 1030, 'end_ms': 1035, 'reason': 'seam'}]
    assert r2['original_microcoulomb'] == '31175/2'

    # Corrections adjust only the receipt they target; order and replays are safe.
    fix = material('correction-handover.json', 'bench-switch-a')
    assert correct(db, fix) == {'status': 'applied', 'correction': 'fix-handover-point',
                                'delta_microcoulomb': '-50'}
    assert correct(db, {'run': 'bench-switch-a', 'id': 'recalibrate-10', 'receipt': 'r1', 'items': [
        {'device': 'primary', 'boot': 1, 'ms': 10, 'rev': 3, 'current_ma': 215}]})['delta_microcoulomb'] == '150'
    # A lower revision arriving late is inert instead of double-deducting.
    assert correct(db, {'run': 'bench-switch-a', 'id': 'late-recal-10', 'receipt': 'r1', 'items': [
        {'device': 'primary', 'boot': 1, 'ms': 10, 'rev': 2, 'current_ma': 210}]})['delta_microcoulomb'] == '0'
    assert correct(db, fix)['status'] == 'replay'
    r1 = next(r for r in settle(db, 'bench-switch-a')['receipts'] if r['id'] == 'r1')
    assert r1['original_microcoulomb'] == '7500' and r1['current_microcoulomb'] == '7600'
    assert [(c['id'], c['delta_microcoulomb']) for c in r1['corrections']] == [
        ('fix-handover-point', '-50'), ('recalibrate-10', '150'), ('late-recal-10', '0')]
    fails(lambda: correct(db, {'run': 'bench-switch-a', 'id': 'c-x', 'receipt': 'r9', 'items': [
        {'device': 'primary', 'boot': 1, 'ms': 10, 'rev': 2, 'current_ma': 1}]}), 'unknown receipt')
    fails(lambda: correct(db, {'run': 'bench-switch-a', 'id': 'c-y', 'receipt': 'r1', 'items': [
        {'device': 'backup', 'boot': 1, 'ms': 10, 'rev': 2, 'current_ma': 1}]}), 'outside receipt coverage')
    fails(lambda: correct(db, {'run': 'bench-switch-a', 'id': 'c-z', 'receipt': 'r1', 'items': [
        {'device': 'primary', 'boot': 1, 'ms': 30, 'rev': 2, 'current_ma': 999}]}), 'conflicting correction')

    # Seam proof arrives: the seam interval seals as new coverage and is signed apart.
    assert add_seam(db, material('seam-primary-backup.json', 'bench-switch-a')) == 'applied'
    assert add_seam(db, material('seam-primary-backup.json', 'bench-switch-a')) == 'replay'
    out = query(db, 'bench-switch-a')
    assert out['pending'] == [] and len(out['segments']) == 11
    assert out['net_microcoulomb'] == '40425'
    assert sign(db, 'bench-switch-a', 1030, 1035) == {
        'status': 'signed', 'receipt': 'r4', 'charge_microcoulomb': '4075/2'}
    view = settle(db, 'bench-switch-a')
    assert view['net_settled_microcoulomb'] == '40525'
    assert view['new_coverage'] == [{'start_ms': 1030, 'end_ms': 1035,
                                     'charge_microcoulomb': '4075/2', 'receipt': 'r4'}]
    assert view['new_coverage_microcoulomb'] == '4075/2'
    assert {r['id']: r['supplementary'] for r in view['receipts']} == {
        'r1': False, 'r2': False, 'r3': False, 'r4': True}
    # A correction on the shared boundary point adjusts only its own receipt;
    # the supplementary receipt keeps the original signed value.
    assert correct(db, {'run': 'bench-switch-a', 'id': 'trim-handover-again', 'receipt': 'r1', 'items': [
        {'device': 'primary', 'boot': 1, 'ms': 30, 'rev': 3, 'current_ma': 380}]})['delta_microcoulomb'] == '-50'
    view = settle(db, 'bench-switch-a')
    by_id = {r['id']: r for r in view['receipts']}
    assert by_id['r1']['current_microcoulomb'] == '7550'
    assert by_id['r4']['current_microcoulomb'] == '4075/2'
    assert view['net_settled_microcoulomb'] == '40475'

    # --- Handover, option (b): wait for a consistent handover -----------------
    handover_materials('bench-switch-b')
    handover_chunks('bench-switch-b')
    assert sign(db, 'bench-switch-b', 1000, 1030)['receipt'] == 'r1'
    assert correct(db, material('correction-handover.json', 'bench-switch-b'))['delta_microcoulomb'] == '-50'
    view = settle(db, 'bench-switch-b')
    # The provable backup interval stays unsettled; the unrelated tail does not wait.
    assert len(view['unsigned_segments']) == 7
    assert sign(db, 'bench-switch-b', 1070, 1100)['charge_microcoulomb'] == '15300'
    assert add_seam(db, material('seam-primary-backup.json', 'bench-switch-b')) == 'applied'
    assert sign(db, 'bench-switch-b', 1030, 1070) == {
        'status': 'signed', 'receipt': 'r3', 'charge_microcoulomb': '17625'}
    view = settle(db, 'bench-switch-b')
    assert view['net_settled_microcoulomb'] == '40375'
    assert [r['id'] for r in view['receipts']] == ['r1', 'r3', 'r2']
    r3 = next(r for r in view['receipts'] if r['id'] == 'r3')
    assert r3['supplementary'] and r3['original_microcoulomb'] == '17625'
    assert view['new_coverage_microcoulomb'] == '4075/2'
    assert view['unsigned_segments'] == [] and view['pending_windows'] == []

    # --- Reboot: two boot sessions report the same local first millisecond ----
    declare_priority(db, {'run': 'bench-reboot', 'priorities': [
        {'device': 'primary', 'boot': 1, 'rank': 1}, {'device': 'primary', 'boot': 2, 'rank': 2}]})
    add_anchor(db, {'run': 'bench-reboot', 'device': 'primary', 'boot': 2,
                    'local_ms': 0, 'canonical_ms': 1000, 'valid_from_ms': 0, 'valid_to_ms': 500})
    add_availability(db, {'run': 'bench-reboot', 'device': 'primary', 'boot': 1,
                          'from_ms': 0, 'to_ms': 60, 'available': True})
    add_availability(db, {'run': 'bench-reboot', 'device': 'primary', 'boot': 1,
                          'from_ms': 60, 'to_ms': 1020, 'available': False})
    add_availability(db, {'run': 'bench-reboot', 'device': 'primary', 'boot': 2,
                          'from_ms': 1000, 'to_ms': 1020, 'available': True})
    ingest(db, {'run': 'bench-reboot', 'chunk': 'boot-1', 'device': 'primary', 'boot': 1, 'seal_ms': 50,
                'samples': [{'ms': ms, 'rev': 1, 'current_ma': 100} for ms in range(0, 51, 10)]}, gap_ms=12)
    ingest(db, {'run': 'bench-reboot', 'chunk': 'boot-2', 'device': 'primary', 'boot': 2, 'seal_ms': 20,
                'samples': [{'ms': ms, 'rev': 1, 'current_ma': 200} for ms in range(0, 21, 10)]}, gap_ms=12)
    out = query(db, 'bench-reboot')
    assert len([s for s in out['samples'] if s['ms'] == 0]) == 2
    assert out['net_microcoulomb'] == '9000' and len(out['segments']) == 7
    assert out['pending'] == [{'start_ms': 50, 'end_ms': 1000, 'reason': 'undecided',
                               'missing_seam_ms': [], 'left_device': 'primary', 'left_boot': 1,
                               'right_device': 'primary', 'right_boot': 2}]
    assert [(w['device'], w['boot']) for w in out['progress']] == [('primary', 1), ('primary', 2)]

    # --- Coincident position and values still do not splice -------------------
    declare_priority(db, {'run': 'bench-coincide', 'priorities': [
        {'device': 'primary', 'boot': 1, 'rank': 1}, {'device': 'backup', 'boot': 1, 'rank': 2}]})
    add_anchor(db, {'run': 'bench-coincide', 'device': 'primary', 'boot': 1,
                    'local_ms': 490, 'canonical_ms': 490, 'valid_from_ms': 0, 'valid_to_ms': 1000})
    add_anchor(db, {'run': 'bench-coincide', 'device': 'backup', 'boot': 1,
                    'local_ms': 0, 'canonical_ms': 490, 'valid_from_ms': 0, 'valid_to_ms': 1000})
    add_availability(db, {'run': 'bench-coincide', 'device': 'primary', 'boot': 1,
                          'from_ms': 0, 'to_ms': 500, 'available': True})
    add_availability(db, {'run': 'bench-coincide', 'device': 'primary', 'boot': 1,
                          'from_ms': 500, 'to_ms': 1000, 'available': False})
    add_availability(db, {'run': 'bench-coincide', 'device': 'backup', 'boot': 1,
                          'from_ms': 500, 'to_ms': 1000, 'available': True})
    samples = [{'ms': 490, 'rev': 1, 'current_ma': 700}, {'ms': 500, 'rev': 1, 'current_ma': 800}]
    ingest(db, {'run': 'bench-coincide', 'chunk': 'p-1', 'device': 'primary', 'boot': 1,
                'seal_ms': 500, 'samples': samples}, gap_ms=12)
    ingest(db, {'run': 'bench-coincide', 'chunk': 'b-1', 'device': 'backup', 'boot': 1, 'seal_ms': 20,
                'samples': [{'ms': 0, 'rev': 1, 'current_ma': 700}, {'ms': 10, 'rev': 1, 'current_ma': 800},
                            {'ms': 20, 'rev': 1, 'current_ma': 800}]},
           gap_ms=12)
    out = query(db, 'bench-coincide')
    assert out['pending'] == [{'start_ms': 500, 'end_ms': 500, 'reason': 'seam',
                               'missing_seam_ms': [500], 'left_device': 'primary', 'left_boot': 1,
                               'right_device': 'backup', 'right_boot': 1}]
    assert out['net_microcoulomb'] == '15500'
    add_seam(db, {'run': 'bench-coincide', 'at_ms': 500,
                  'left': {'device': 'primary', 'boot': 1}, 'right': {'device': 'backup', 'boot': 1}})
    out = query(db, 'bench-coincide')
    assert out['pending'] == []
    seam = next(s for s in out['segments'] if s['kind'] == 'seam')
    assert (seam['start_ms'], seam['end_ms'], seam['charge_microcoulomb']) == (500, 500, '0')

    print(json.dumps(out, sort_keys=True))
