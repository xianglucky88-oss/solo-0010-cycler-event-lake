"""Settlement of work steps against independent coulometers.

The runtime lake answers *what the bench sampled*.  Settlement asks a
different question: *what can still be true about a work step*, given sealed
samples, sealed meter windows, declared charge/discharge capability during a
missing measurement, and the relation between the test clock and the
settlement clock.

Rules encoded here
------------------

* Every charge is an exact :class:`fractions.Fraction`; nothing is rounded.
* A meter window constrains **physical** charge.  Intersecting windows and
  several steps over one gap therefore share the same variables and are
  counted once; an extreme of one sheet is never pasted onto another's.
* Meter-backed charge is never reported as observed charge.  Observed charge
  comes only from sealed, integrated sample pairs.
* Net zero does not mean nothing moved; charge-in and charge-out are kept
  apart.
* A sample pair, a step or a meter window may cut through one another.  The
  charge of a partial piece is derived from the *same* linear interpolation
  between the two sampling endpoints (the same endpoint basis), never from a
  separate estimate.
* An unsealed window (``seal_ms`` before its end, or ``-1``) is evidence of
  nothing and never constrains a step.
* Bills are append-only.  Later evidence may verify or contradict an old
  bill; it never silently rewrites or withdraws it.
"""
from fractions import Fraction
import json
from math import inf

from .lp import linprog

ZERO = Fraction(0)
INF = inf
# A window with an explicit ``seal_ms`` at or beyond its end is confirmed.
# Omitting ``seal_ms`` means "this readout is already final".
SEAL_FULL = 4 * 10 ** 18

# Step statuses ------------------------------------------------------------
STATUS_OBSERVED = 'observed'            # fully measured integration
STATUS_MISSING = 'missing'              # confirmed missing, no meter evidence
STATUS_METER_PENDING = 'meter_pending'  # meter evidence, not yet signable
STATUS_METER_READY = 'meter_ready'      # meter evidence, exactly determined
STATUS_UNSEALED = 'unsealed'            # reaches past the sealed watermark
STATUS_CONTRADICTION = 'contradiction'  # sealed facts cannot all hold
STATUS_CONFLICT = 'conflict'            # a signed bill clashes with new facts


# ---------------------------------------------------------------------------
# Exact parsing


def _signed(value, field):
    if isinstance(value, Fraction):
        return value
    if type(value) is int:
        return Fraction(value)
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError(f'{field} is not an exact rational')
    raise ValueError(f'{field} must be an integer or rational string')


def _nonnegative(value, field):
    result = _signed(value, field)
    if result < 0:
        raise ValueError(f'{field} must not be negative')
    return result


def _pair(value):
    return value.numerator, value.denominator


def _text(value):
    if value is None or value == INF:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# Configuration


def configure(path, config):
    """Install clock relations, steps, meter windows and outage capability."""
    from .lake import _database

    for name in ('clock', 'steps', 'windows', 'outages'):
        if not isinstance(config.get(name, []), list):
            raise ValueError(f'{name} must be an array')
    clock = config.get('clock', [])
    steps = config.get('steps', [])
    windows = config.get('windows', [])
    outages = config.get('outages', [])
    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        for item in clock:
            offset = _signed(item.get('offset', 0), 'clock offset')
            scale = _signed(item.get('scale', 1), 'clock scale')
            if scale <= 0:
                raise ValueError('clock scale must be positive')
            c.execute('REPLACE INTO clock_map VALUES(?,?,?,?,?)',
                      (item['run'], *_pair(offset), *_pair(scale)))
        for item in steps:
            start, end = item['start_ms'], item['end_ms']
            if type(start) is not int or type(end) is not int or start > end:
                raise ValueError('step bounds must be integers with start <= end')
            c.execute('REPLACE INTO work_step VALUES(?,?,?,?,?)',
                      (item['id'], item['run'], item.get('label'), start, end))
        for item in windows:
            start = _nonnegative(item['start'], 'window start')
            end = _nonnegative(item['end'], 'window end')
            if end < start:
                raise ValueError('window ends before it starts')
            charge_in = _nonnegative(item['in'], 'window in')
            charge_out = _nonnegative(item['out'], 'window out')
            error = _nonnegative(item.get('error', 0), 'window error')
            seal = item.get('seal_ms', SEAL_FULL)
            if type(seal) is not int or seal < -1:
                raise ValueError('window seal_ms must be an integer >= -1')
            runs = item.get('runs', [])
            if not isinstance(runs, list) or not runs:
                raise ValueError('window must cover at least one run')
            c.execute('REPLACE INTO meter_window VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (item['id'], seal, *_pair(start), *_pair(end),
                       *_pair(charge_in), *_pair(charge_out), *_pair(error)))
            c.execute('DELETE FROM meter_window_run WHERE window=?', (item['id'],))
            for run in runs:
                c.execute('INSERT INTO meter_window_run VALUES(?,?)', (item['id'], run))
        for item in outages:
            start, end = item['start_ms'], item['end_ms']
            if type(start) is not int or type(end) is not int or start > end:
                raise ValueError('outage bounds must be integers with start <= end')
            cap_in = _nonnegative(item.get('max_in_ma', 0), 'max_in_ma')
            cap_out = _nonnegative(item.get('max_out_ma', 0), 'max_out_ma')
            c.execute('REPLACE INTO outage VALUES(?,?,?,?,?,?,?,?)',
                      (item['id'], item['run'], start, end, *_pair(cap_in), *_pair(cap_out)))
    return {'status': 'configured', 'clock': len(clock), 'steps': len(steps),
            'windows': len(windows), 'outages': len(outages)}


def _clocks(conn):
    return {row['id']: (Fraction(row['offset_num'], row['offset_den']),
                        Fraction(row['scale_num'], row['scale_den']))
            for row in conn.execute('SELECT * FROM clock_map')}


def _to_test(clocks, run, settlement_ms):
    offset, scale = clocks.get(run, (ZERO, Fraction(1)))
    return (settlement_ms - offset) / scale


# ---------------------------------------------------------------------------
# Geometry: atoms of equal evidentiary status


def _current_at(samples, at):
    for (t0, i0), (t1, i1) in zip(samples, samples[1:]):
        if t0 <= at <= t1:
            if t1 == t0:
                return i0
            return i0 + (i1 - i0) * (at - t0) / (t1 - t0)
    raise ValueError('time outside the sampled span')


def _split_area(left, right, dt):
    """Positive and negative areas of a linear current over ``dt``."""
    total = (left + right) * dt / 2
    if left >= 0 and right >= 0:
        return total, ZERO
    if left <= 0 and right <= 0:
        return ZERO, -total
    zero_at = left * dt / (left - right)
    if left > 0:
        positive = left * zero_at / 2
        return positive, positive - total
    negative = left * zero_at / 2
    return total - negative, -negative


def _samples_at(conn, run):
    return [(Fraction(row['ms']), Fraction(row['ma']))
            for row in conn.execute('SELECT ms,ma FROM sample WHERE run=? ORDER BY ms', (run,))]


def _window_rows(conn):
    clocks_run = None
    result = []
    for row in conn.execute('SELECT * FROM meter_window ORDER BY id'):
        runs = [item['run'] for item in conn.execute(
            'SELECT run FROM meter_window_run WHERE window=? ORDER BY run', (row['id'],))]
        item = dict(row)
        item.update({
            'runs': runs,
            'start': Fraction(row['start_num'], row['start_den']),
            'end': Fraction(row['end_num'], row['end_den']),
            'read_in': Fraction(row['in_num'], row['in_den']),
            'read_out': Fraction(row['out_num'], row['out_den']),
            'error': Fraction(row['error_num'], row['error_den']),
        })
        # "confirmed" means the independent meter has sealed this whole window.
        item['confirmed'] = item['seal_ms'] >= 0 and Fraction(item['seal_ms']) >= item['end']
        result.append(item)
    return result


def _outage_rows(conn, run):
    return [dict(row) for row in conn.execute('SELECT * FROM outage WHERE run=? ORDER BY id', (run,))]


def _enclosing_pair(samples, start, end):
    for index in range(len(samples) - 1):
        if samples[index][0] <= start and end <= samples[index + 1][0]:
            return index
    return None


def _geometry(conn, run, gap_ms, windows, clocks, steps):
    state = conn.execute('SELECT seal_ms FROM run_state WHERE id=?', (run,)).fetchone()
    if state is None:
        return None
    seal = Fraction(state['seal_ms'])
    samples = _samples_at(conn, run)
    outages = _outage_rows(conn, run)

    bounds = set()
    for step in steps:
        if step['run'] == run:
            bounds.update((Fraction(step['start_ms']), Fraction(step['end_ms'])))
    for window in windows:
        if run in window['runs']:
            bounds.add(_to_test(clocks, run, window['start']))
            bounds.add(_to_test(clocks, run, window['end']))
    for outage in outages:
        bounds.update((Fraction(outage['start_ms']), Fraction(outage['end_ms'])))
    for at, _ in samples:
        bounds.add(at)
    if seal >= 0:
        bounds.add(seal)
    if not bounds:
        return {'run': run, 'seal': seal, 'samples': samples, 'outages': outages, 'atoms': []}

    points = sorted(bounds)
    atoms = []
    for start, end in zip(points, points[1:]):
        if start >= end:
            continue
        kind = 'unsealed'
        obs_in = obs_out = ZERO
        if seal >= 0 and end <= seal:
            kind = 'gap'
            index = _enclosing_pair(samples, start, end)
            if index is not None:
                t0, _ = samples[index]
                t1, _ = samples[index + 1]
                # The lake freezes a pair only once the watermark reaches its
                # right endpoint; a pair that pokes past the seal is not a fact.
                if t1 <= seal and t1 - t0 <= gap_ms:
                    kind = 'exact'
                    left = _current_at(samples, start)
                    right = _current_at(samples, end)
                    obs_in, obs_out = _split_area(left, right, end - start)
        atoms.append({'run': run, 'id': len(atoms), 'start': start, 'end': end,
                      'kind': kind, 'obs_in': obs_in, 'obs_out': obs_out})
    return {'run': run, 'seal': seal, 'samples': samples, 'outages': outages, 'atoms': atoms}


def _overlap(atom, lo, hi):
    return max(ZERO, min(atom['end'], hi) - max(atom['start'], lo))


# ---------------------------------------------------------------------------
# Joint linear program over shared physical charge


class _Component:
    """One independent block of coupled variables."""

    def __init__(self, size, rows, local):
        self.size = size
        self.local = local
        self.A = [[row.get(position, ZERO) for position in range(size)] for row, _ in rows]
        self.b = [rhs for _, rhs in rows]
        self._state = None

    def state(self):
        if self._state is None:
            self._state = linprog(self.A, self.b, [ZERO] * self.size)[0]
        return self._state

    def feasible(self):
        return self.state() != 'infeasible'

    def limit(self, objective, maximise):
        if not self.feasible():
            return None
        vector = [objective.get(position, ZERO) for position in range(self.size)]
        if not maximise:
            vector = [-value for value in vector]
        state, value, _ = linprog(self.A, self.b, vector)
        if state != 'optimal':
            return INF if state == 'unbounded' else None
        return value if maximise else -value


def _build(conn, default_gap):
    clocks = _clocks(conn)
    steps = [dict(row) for row in conn.execute('SELECT * FROM work_step ORDER BY start_ms,id')]
    windows = _window_rows(conn)
    runs = []
    seen = set()
    for run in ([step['run'] for step in steps]
                + [row['id'] for row in conn.execute('SELECT id FROM run_state ORDER BY id')]
                + [run for window in windows for run in window['runs']]):
        if run not in seen:
            seen.add(run)
            runs.append(run)

    geometries = {}
    for run in runs:
        state = conn.execute('SELECT gap_ms FROM run_state WHERE id=?', (run,)).fetchone()
        gap = state['gap_ms'] if state and state['gap_ms'] is not None else default_gap
        geometries[run] = _geometry(conn, run, gap, windows, clocks, steps)
    geometries = {run: geometry for run, geometry in geometries.items() if geometry is not None}

    atoms = [atom for geometry in geometries.values() for atom in geometry['atoms']]
    free = [atom for atom in atoms if atom['kind'] != 'exact']

    # Observed (exact) charge is a constant, never a variable.  A confirmed
    # window that overlaps sealed sampling folds that charge into its own
    # bounds; this keeps an observation an observation and the LP small.
    index = {}
    for atom in free:
        index[(atom['run'], atom['id'], 'in')] = len(index)
        index[(atom['run'], atom['id'], 'out')] = len(index)

    groups = []
    # Declared charge/discharge capability during missing measurement.
    for run, geometry in geometries.items():
        for outage in geometry['outages']:
            lo, hi = Fraction(outage['start_ms']), Fraction(outage['end_ms'])
            cap_in = Fraction(outage['max_in_num'], outage['max_in_den'])
            cap_out = Fraction(outage['max_out_num'], outage['max_out_den'])
            rows, members = [], set()
            for atom in geometry['atoms']:
                overlap = _overlap(atom, lo, hi)
                if overlap <= 0 or atom['kind'] == 'exact':
                    continue
                members.add((atom['run'], atom['id']))
                rows.append(({index[(atom['run'], atom['id'], 'in')]: Fraction(1)}, cap_in * overlap))
                rows.append(({index[(atom['run'], atom['id'], 'out')]: Fraction(1)}, cap_out * overlap))
            if rows:
                groups.append({'kind': 'outage', 'id': f'outage:{outage["id"]}',
                               'rows': rows, 'atoms': members})
    # Independent meter windows.
    for window in windows:
        if not window['confirmed']:
            continue
        in_row, out_row, members = {}, {}, set()
        sampled_in = sampled_out = ZERO
        for run in window['runs']:
            geometry = geometries.get(run)
            if geometry is None:
                continue
            lo = _to_test(clocks, run, window['start'])
            hi = _to_test(clocks, run, window['end'])
            for atom in geometry['atoms']:
                if _overlap(atom, lo, hi) <= 0:
                    continue
                key = (atom['run'], atom['id'])
                if atom['kind'] == 'exact':
                    sampled_in += atom['obs_in']
                    sampled_out += atom['obs_out']
                    continue
                members.add(key)
                in_row[index[(atom['run'], atom['id'], 'in')]] = Fraction(1)
                out_row[index[(atom['run'], atom['id'], 'out')]] = Fraction(1)
        low_in = window['read_in'] - window['error'] - sampled_in
        high_in = window['read_in'] + window['error'] - sampled_in
        low_out = window['read_out'] - window['error'] - sampled_out
        high_out = window['read_out'] + window['error'] - sampled_out
        rows = [(dict(in_row), high_in),
                ({k: -v for k, v in in_row.items()}, -low_in),
                (dict(out_row), high_out),
                ({k: -v for k, v in out_row.items()}, -low_out)]
        groups.append({'kind': 'meter', 'id': f'meter:{window["id"]}',
                       'rows': rows, 'atoms': members})
        # Residual bounds on free (unobserved) charge in this window.
        # ``members`` empty means there is no unobserved time to absorb any
        # residual, so only residual == 0 fits.
        constant_conflict = False
        if not members and (low_in > 0 or low_out > 0 or high_in < 0 or high_out < 0):
            constant_conflict = True
        if (sampled_in > window['read_in'] + window['error']
                or sampled_out > window['read_out'] + window['error']):
            constant_conflict = True
        if constant_conflict:
            sampled_pairs = [
                {'run': atom['run'], 'start_ms': int(atom['start']), 'end_ms': int(atom['end']),
                 'in_microcoulomb': str(atom['obs_in']),
                 'out_microcoulomb': str(atom['obs_out'])}
                for atom in atoms
                if atom['kind'] == 'exact' and atom['run'] in window['runs']
                and _overlap(atom, _to_test(clocks, atom['run'], window['start']),
                             _to_test(clocks, atom['run'], window['end'])) > 0]
            window['sampled_over'] = {'in': sampled_in, 'out': sampled_out,
                                      'read_in': window['read_in'], 'read_out': window['read_out'],
                                      'error': window['error'],
                                      'residual': [str(low_in), str(high_in)],
                                      'residual_out': [str(low_out), str(high_out)],
                                      'sampled_pairs': sampled_pairs}

    parent = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for atom in atoms:
        find((atom['run'], atom['id']))
    for group in groups:
        members = list(group['atoms'])
        for other in members[1:]:
            union(members[0], other)

    components, solvers = {}, {}
    for atom in atoms:
        components.setdefault(find((atom['run'], atom['id'])), []).append(atom)
    for root, members in components.items():
        member_keys = {(atom['run'], atom['id']) for atom in members}
        local_variables = [index[key] for key in index if (key[0], key[1]) in member_keys]
        rows = []
        for group in groups:
            if group['atoms'] & member_keys:
                rows.extend(group['rows'])
        local = {global_index: position for position, global_index in enumerate(local_variables)}
        local_rows = [({local[key]: value for key, value in row.items() if key in local}, rhs)
                      for row, rhs in rows]
        solvers[root] = _Component(len(local_variables), local_rows, local)

    return {'steps': steps, 'windows': windows, 'runs': runs, 'geometries': geometries,
            'atoms': atoms, 'free': free, 'components': components, 'solvers': solvers,
            'groups': groups, 'index': index, 'clocks': clocks, 'conn': conn,
            'gap': default_gap,
            'root_of': {(atom['run'], atom['id']): find((atom['run'], atom['id'])) for atom in atoms}}

# ---------------------------------------------------------------------------
# Per-step readings


def _step_atoms(problem, step):
    geometry = problem['geometries'].get(step['run'])
    if geometry is None:
        return [], []
    lo, hi = Fraction(step['start_ms']), Fraction(step['end_ms'])
    inside = [atom for atom in geometry['atoms'] if atom['start'] >= lo and atom['end'] <= hi]
    return inside, [atom for atom in inside if atom['kind'] != 'exact']


def _roots(problem, atoms):
    return {problem['root_of'][(atom['run'], atom['id'])] for atom in atoms}


def _extreme(problem, root, free_atoms, maximise, weights):
    """Maximise/minimise a weighted sum of the free atoms in one component.

    ``weights`` maps a side (``'in'``/``'out'``) to its coefficient, so a joint
    quantity such as net charge (``in - out``) is optimised as a whole instead
    of summing two independent extremes.
    """
    solver = problem['solvers'][root]
    objective = {}
    for atom in free_atoms:
        for side, coefficient in weights.items():
            variable = problem['index'][(atom['run'], atom['id'], side)]
            if variable in solver.local:
                position = solver.local[variable]
                objective[position] = objective.get(position, ZERO) + coefficient
    return solver.limit(objective, maximise)


def _ranges(problem, step):
    inside, free_atoms = _step_atoms(problem, step)
    observed_in = sum((atom['obs_in'] for atom in inside if atom['kind'] == 'exact'), ZERO)
    observed_out = sum((atom['obs_out'] for atom in inside if atom['kind'] == 'exact'), ZERO)
    low_in = high_in = observed_in
    low_out = high_out = observed_out
    low_net = high_net = observed_in - observed_out
    feasible = True
    blockers = set()
    for root in _roots(problem, inside):
        solver = problem['solvers'][root]
        if not solver.feasible():
            feasible = False
            blockers.add(root)
            continue
        members = [atom for atom in free_atoms if problem['root_of'][(atom['run'], atom['id'])] == root]
        if not members:
            continue
        values = (
            _extreme(problem, root, members, False, {'in': Fraction(1)}),
            _extreme(problem, root, members, True, {'in': Fraction(1)}),
            _extreme(problem, root, members, False, {'out': Fraction(1)}),
            _extreme(problem, root, members, True, {'out': Fraction(1)}),
            # Signed net is a joint quantity: in - out, not one extreme of each.
            _extreme(problem, root, members, False, {'in': Fraction(1), 'out': Fraction(-1)}),
            _extreme(problem, root, members, True, {'in': Fraction(1), 'out': Fraction(-1)}),
        )
        if None in values:
            feasible = False
            blockers.add(root)
            continue
        low_in += values[0]
        high_in += values[1]
        low_out += values[2]
        high_out += values[3]
        low_net += values[4]
        high_net += values[5]
    return {'inside': inside, 'observed_in': observed_in, 'observed_out': observed_out,
            'low_in': low_in, 'high_in': high_in, 'low_out': low_out, 'high_out': high_out,
            'low_net': low_net, 'high_net': high_net, 'feasible': feasible,
            'blockers': blockers,
            'sampled_over': [window['id'] for window in problem['windows']
                             if _window_hits_step(problem, step, window)
                             and window.get('sampled_over')]}


def _window_hits_step(problem, step, window):
    geometry = problem['geometries'].get(step['run'])
    if geometry is None or step['run'] not in window['runs'] or not window['confirmed']:
        return False
    lo = _to_test(problem['clocks'], step['run'], window['start'])
    hi = _to_test(problem['clocks'], step['run'], window['end'])
    inside, _ = _step_atoms(problem, step)
    return any(_overlap(atom, lo, hi) > 0 for atom in inside)


def _covered(problem, step, atom):
    """True when a confirmed independent window covers this atom's span."""
    for window in problem['windows']:
        if not window['confirmed'] or step['run'] not in window['runs']:
            continue
        lo = _to_test(problem['clocks'], step['run'], window['start'])
        hi = _to_test(problem['clocks'], step['run'], window['end'])
        if _overlap(atom, lo, hi) >= atom['end'] - atom['start']:
            return True
    return False


def _covers_meter(problem, step, inside):
    free_atoms = [atom for atom in inside if atom['kind'] != 'exact']
    return any(_covered(problem, step, atom) for atom in free_atoms)


def _status(problem, step, inside, feasible, determined, sampled_over):
    if not feasible or sampled_over:
        return STATUS_CONTRADICTION
    if not inside:
        return STATUS_UNSEALED
    # A step reaching past the sampling watermark is not a fact yet, unless a
    # confirmed independent meter covers that reach.
    uncovered = [atom for atom in inside
                 if atom['kind'] == 'unsealed' and not _covered(problem, step, atom)]
    if uncovered:
        return STATUS_UNSEALED
    if all(atom['kind'] == 'exact' for atom in inside):
        return STATUS_OBSERVED
    if _covers_meter(problem, step, inside):
        return STATUS_METER_READY if determined else STATUS_METER_PENDING
    return STATUS_MISSING


# ---------------------------------------------------------------------------
# Conflict diagnosis


def _infeasible_with(problem, member_keys, groups):
    variables = [index for key, index in problem['index'].items() if (key[0], key[1]) in member_keys]
    local = {global_index: position for position, global_index in enumerate(variables)}
    rows = []
    for group in groups:
        for row, rhs in group['rows']:
            rows.append(({local[key]: value for key, value in row.items() if key in local}, rhs))
    return not _Component(len(variables), rows, local).feasible()


def _conflict_core(problem, blocker):
    """A minimal set of contradictory evidence for one infeasible component.

    Groups are dropped one at a time while the component stays infeasible, so
    the survivors are a minimal witness, not merely "everything in the block".
    """
    member_keys = {(atom['run'], atom['id']) for atom in problem['components'][blocker]}
    candidates = [group for group in problem['groups'] if group['atoms'] & member_keys]
    core = list(candidates)
    position = 0
    while position < len(core):
        trial = core[:position] + core[position + 1:]
        if _infeasible_with(problem, member_keys, trial):
            core = trial
        else:
            position += 1
    return [{'kind': group['kind'], 'id': group['id']} for group in core]


# ---------------------------------------------------------------------------
# Bills (append-only)


def _bill_row(row):
    return {'step': row['step'], 'seq': row['seq'], 'run': row['run'],
            'in': Fraction(row['in_num'], row['in_den']),
            'out': Fraction(row['out_num'], row['out_den']),
            'net': Fraction(row['net_num'], row['net_den']),
            'signed_seal_ms': row['signed_seal_ms'], 'note': row['note']}


def _latest_bills(conn, step=None):
    sql = ('SELECT b.* FROM bill b JOIN (SELECT step AS s, MAX(seq) AS m FROM bill GROUP BY step) latest '
           'ON latest.s=b.step AND latest.m=b.seq')
    params = ()
    if step is not None:
        sql += ' WHERE b.step=?'
        params = (step,)
    return {row['step']: _bill_row(row) for row in conn.execute(sql + ' ORDER BY b.step', params)}


def _bill_history(conn, step):
    return [_bill_row(row) for row in conn.execute(
        'SELECT * FROM bill WHERE step=? ORDER BY seq', (step,))]


def _bill_view(bill, observed_in, observed_out, low_in, high_in, low_out, high_out,
               feasible, status, sampled_over=False):
    view = {'seq': bill['seq'], 'in_microcoulomb': str(bill['in']),
            'out_microcoulomb': str(bill['out']), 'net_microcoulomb': str(bill['net']),
            'signed_seal_ms': bill['signed_seal_ms']}
    # Later sealed sampling can directly contradict a signed amount even when
    # the wider evidence set is otherwise contradictory: reflect the measured
    # value and the difference, but never touch the bill.
    if status == STATUS_CONTRADICTION and (observed_in or observed_out):
        if (observed_in, observed_out) != (bill['in'], bill['out']):
            view['verification'] = 'conflict'
            view['observed'] = {'in_microcoulomb': str(observed_in),
                                'out_microcoulomb': str(observed_out)}
            view['conflict'] = {
                'observed_in_microcoulomb': str(observed_in),
                'observed_out_microcoulomb': str(observed_out),
                'observed_minus_bill_in': str(observed_in - bill['in']),
                'observed_minus_bill_out': str(observed_out - bill['out']),
                'note': 'measured integration differs from the signed bill'}
            view['sealed_facts_conflict'] = True
            return view, True
        view['verification'] = 'unverifiable'
        view['reason'] = 'sealed sampling and meter readings cannot all hold'
        return view, False
    if not feasible:
        # The run cannot vouch for the bill, but the bill is not the thing in
        # conflict -- sealing facts are.  It stays, and stays unverifiable.
        view['verification'] = 'unverifiable'
        view['reason'] = 'sealed sampling and meter readings cannot all hold'
        return view, False
    if status == STATUS_OBSERVED:
        if (observed_in, observed_out) != (bill['in'], bill['out']):
            view['verification'] = 'conflict'
            view['conflict'] = {
                'observed_in_microcoulomb': str(observed_in),
                'observed_out_microcoulomb': str(observed_out),
                'observed_minus_bill_in': str(observed_in - bill['in']),
                'observed_minus_bill_out': str(observed_out - bill['out'])}
            return view, True
    elif not sampled_over and not (low_in <= bill['in'] <= high_in and low_out <= bill['out'] <= high_out):
        view['verification'] = 'conflict'
        view['conflict'] = {
            'possible_in_microcoulomb': [_text(low_in), _text(high_in)],
            'possible_out_microcoulomb': [_text(low_out), _text(high_out)]}
        return view, True
    view['verification'] = 'verified'
    return view, False


# ---------------------------------------------------------------------------
# Reports


def _witness(problem, step, inside):
    items = []
    seen = set()
    for atom in inside:
        if atom['kind'] != 'exact':
            continue
        key = (atom['start'], atom['end'])
        if key in seen:
            continue
        seen.add(key)
        items.append({'type': 'sealed_sampling', 'start_ms': int(atom['start']),
                      'end_ms': int(atom['end']), 'in_microcoulomb': str(atom['obs_in']),
                      'out_microcoulomb': str(atom['obs_out'])})
    for window in problem['windows']:
        if step['run'] not in window['runs']:
            continue
        lo = _to_test(problem['clocks'], step['run'], window['start'])
        hi = _to_test(problem['clocks'], step['run'], window['end'])
        if any(_overlap(atom, lo, hi) > 0 for atom in inside):
            items.append({'type': 'sealed_meter' if window['confirmed'] else 'unsealed_meter',
                          'window': window['id'], 'start': str(window['start']),
                          'end': str(window['end']),
                          'read_in_microcoulomb': str(window['read_in']),
                          'read_out_microcoulomb': str(window['read_out']),
                          'error_microcoulomb': str(window['error'])})
    geometry = problem['geometries'].get(step['run'])
    for outage in (geometry['outages'] if geometry else []):
        lo, hi = Fraction(outage['start_ms']), Fraction(outage['end_ms'])
        if any(atom['kind'] != 'exact' and _overlap(atom, lo, hi) > 0 for atom in inside):
            items.append({'type': 'missing_capability', 'outage': outage['id'],
                          'start_ms': outage['start_ms'], 'end_ms': outage['end_ms'],
                          'max_in_ma': str(Fraction(outage['max_in_num'], outage['max_in_den'])),
                          'max_out_ma': str(Fraction(outage['max_out_num'], outage['max_out_den']))})
    return items


def _links(problem):
    """Steps coupled by shared physical charge, including across tests/windows."""
    parent = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    by_root = {}
    for step in problem['steps']:
        find(step['id'])
        inside, _ = _step_atoms(problem, step)
        for root in _roots(problem, inside):
            by_root.setdefault(root, []).append(step['id'])
    for members in by_root.values():
        for other in members[1:]:
            union(members[0], other)
    groups = {}
    for step in problem['steps']:
        groups.setdefault(find(step['id']), []).append(step['id'])
    return [{'members': sorted(group)} for group in groups.values() if len(group) > 1]


def _report(problem, conn, step_filter=None):
    bills = _latest_bills(conn)
    reports = []
    for step in problem['steps']:
        if step_filter is not None and step['id'] != step_filter:
            continue
        reading = _ranges(problem, step)
        inside = reading['inside']
        observed_in, observed_out = reading['observed_in'], reading['observed_out']
        low_in, high_in = reading['low_in'], reading['high_in']
        low_out, high_out = reading['low_out'], reading['high_out']
        feasible, blockers = reading['feasible'], reading['blockers']
        sampled_over = reading['sampled_over']
        determined = (feasible and not sampled_over and bool(inside)
                      and low_in == high_in and low_out == high_out)
        status = _status(problem, step, inside, feasible, determined, sampled_over)
        bounded = feasible and not sampled_over and bool(inside)
        entry = {
            'step': step['id'], 'run': step['run'], 'label': step['label'],
            'start_ms': step['start_ms'], 'end_ms': step['end_ms'], 'status': status,
            'observed': {'in_microcoulomb': str(observed_in), 'out_microcoulomb': str(observed_out)},
            'possible': {'in_microcoulomb': [_text(low_in if bounded else None),
                                             _text(high_in if bounded else None)],
                         'out_microcoulomb': [_text(low_out if bounded else None),
                                              _text(high_out if bounded else None)],
                         'net_microcoulomb': [_text(reading['low_net'] if bounded else None),
                                              _text(reading['high_net'] if bounded else None)]},
            'signable': determined,
            'witness': _witness(problem, step, inside),
        }
        bill = bills.get(step['id'])
        if bill is not None:
            entry['bill'], conflict = _bill_view(bill, observed_in, observed_out, low_in, high_in,
                                                 low_out, high_out, feasible and not bool(sampled_over),
                                                 status, sampled_over=bool(sampled_over))
            if conflict:
                entry['status'] = STATUS_CONFLICT
        if not feasible:
            core = []
            for blocker in sorted(blockers):
                for item in _conflict_core(problem, blocker):
                    if item not in core:
                        core.append(item)
            entry['conflict_core'] = core
        elif sampled_over:
            core = []
            for name in sampled_over:
                window = next(item for item in problem['windows'] if item['id'] == name)
                detail = window['sampled_over']
                for pair in detail['sampled_pairs']:
                    core.append({'kind': 'sample', 'run': pair['run'],
                                 'start_ms': pair['start_ms'], 'end_ms': pair['end_ms'],
                                 'in_microcoulomb': pair['in_microcoulomb'],
                                 'out_microcoulomb': pair['out_microcoulomb']})
                core.append({'kind': 'meter', 'id': f'meter:{name}',
                             'read_in_microcoulomb': str(detail['read_in']),
                             'read_out_microcoulomb': str(detail['read_out']),
                             'error_microcoulomb': str(detail['error']),
                             'free_residual_in': detail['residual'],
                             'free_residual_out': detail['residual_out']})
            entry['conflict_core'] = core
        reports.append(entry)
    return {'steps': reports, 'links': _links(problem)}


def report(path, gap_ms=12, step=None):
    from .lake import _database
    with _database(path) as c:
        return _report(_build(c, gap_ms), c, step_filter=step)


def history(path, step):
    from .lake import _database
    with _database(path) as c:
        return {'step': step, 'bills': [
            {'seq': bill['seq'], 'in_microcoulomb': str(bill['in']),
             'out_microcoulomb': str(bill['out']), 'net_microcoulomb': str(bill['net']),
             'signed_seal_ms': bill['signed_seal_ms'], 'note': bill['note']}
            for bill in _bill_history(c, step)]}


# ---------------------------------------------------------------------------
# Signing


def _targets(problem, conn, step_id, mode):
    full = _report(problem, conn)
    by_id = {entry['step']: entry for entry in full['steps']}
    if step_id not in by_id:
        raise ValueError(f'unknown work step: {step_id}')
    if mode == 'pre':
        return [by_id[step_id]]
    if mode != 'group':
        raise ValueError('mode must be "pre" or "group"')
    for link in full['links']:
        if step_id in link['members']:
            return [by_id[name] for name in link['members']]
    return [by_id[step_id]]


def sign(path, step_id, *, mode='pre', gap_ms=12):
    """Sign determined steps; never rewrite an earlier bill.

    ``pre``   signs the named step when it is identical in every compatible
              explanation, even if a linked group is still open.
    ``group`` waits for the whole linked group and signs it atomically; if the
              group is not fully determined nothing is signed.
    """
    from .lake import _database

    with _database(path) as c:
        c.execute('BEGIN IMMEDIATE')
        problem = _build(c, gap_ms)
        targets = _targets(problem, c, step_id, mode)
        for entry in targets:
            if entry['status'] == STATUS_CONTRADICTION:
                raise ValueError(f"step {entry['step']!r} is contradicted by its sealed facts")
            if entry['status'] == STATUS_UNSEALED:
                raise ValueError(f"step {entry['step']!r} is not fully sealed and has no confirmed meter covering it")
            if entry['status'] == STATUS_CONFLICT:
                raise ValueError(f"step {entry['step']!r} conflicts with an existing bill")
        blocked = [entry['step'] for entry in targets if not entry['signable']]
        if blocked:
            # Not enough evidence yet: the step(s) stay unsettled.
            return {'status': 'pending', 'blocked': blocked,
                    'reason': 'not identical across all compatible explanations'}
        bills = _latest_bills(c)
        signed = []
        for entry in targets:
            value_in = Fraction(entry['possible']['in_microcoulomb'][0])
            value_out = Fraction(entry['possible']['out_microcoulomb'][0])
            prior = bills.get(entry['step'])
            if prior is not None:
                if prior['in'] == value_in and prior['out'] == value_out:
                    signed.append({'step': entry['step'], 'seq': prior['seq'], 'status': 'replay'})
                    continue
                raise ValueError(f'bill for {entry["step"]!r} conflicts with current evidence '
                                 'and is never rewritten silently')
            seq = c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM bill WHERE step=?',
                            (entry['step'],)).fetchone()[0]
            seal = c.execute('SELECT seal_ms FROM run_state WHERE id=?', (entry['run'],)).fetchone()
            note = json.dumps({'policy': mode,
                               'group': sorted(item['step'] for item in targets) if mode == 'group' else None},
                              sort_keys=True)
            c.execute('INSERT INTO bill VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (entry['step'], seq, entry['run'], *_pair(value_in), *_pair(value_out),
                       *_pair(value_in - value_out), seal['seal_ms'] if seal else -1, note))
            signed.append({'step': entry['step'], 'seq': seq, 'status': 'signed',
                           'in_microcoulomb': str(value_in), 'out_microcoulomb': str(value_out),
                           'net_microcoulomb': str(value_in - value_out)})
    return {'status': 'applied', 'bills': signed}


# ---------------------------------------------------------------------------
# Run-level daily query


def query_block(conn, run, gap_ms=12):
    """Run facts that separate observed, missing, meter-backed and signed charge."""
    problem = _build(conn, gap_ms)
    steps = [step for step in problem['steps'] if step['run'] == run]
    if not steps:
        return None
    local = dict(problem, steps=steps)
    result = _report(local, conn)
    ids = {step['id'] for step in steps}
    links = [link for link in _links(problem) if ids & set(link['members'])]
    return {'steps': result['steps'], 'links': links}
