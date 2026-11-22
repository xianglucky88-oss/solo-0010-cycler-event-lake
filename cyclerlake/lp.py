"""Exact rational linear programming (two-phase simplex).

The settlement problem is a small linear program over exact rational charge
quantities.  Floating point would silently make two bills agree or disagree,
so everything here is computed with :class:`fractions.Fraction`.

``linprog(A, b, c)`` maximises ``c . x`` subject to ``A x <= b`` and ``x >= 0``
and returns one of::

    ('optimal', value, x)
    ('infeasible', None, None)
    ('unbounded', None, None)

Bland's rule is used for both entering and leaving variables, which rules out
cycling on degenerate vertices.
"""
from fractions import Fraction


def _as_fraction(value):
    return value if isinstance(value, Fraction) else Fraction(value)


class _Simplex:
    def __init__(self, A, b, width):
        self.m = len(A)
        self.n = width
        negative = [i for i in range(self.m) if b[i] < 0]
        self.artificials = set(range(self.n + self.m, self.n + self.m + len(negative)))
        self.N = self.n + self.m + len(negative)
        self.rhs = self.N
        self.tableau = [[Fraction(0)] * (self.N + 1) for _ in range(self.m)]
        self.basis = [0] * self.m
        art = 0
        for i in range(self.m):
            row = self.tableau[i]
            if b[i] >= 0:
                for j in range(self.n):
                    row[j] = A[i][j]
                row[self.n + i] = Fraction(1)
                row[self.rhs] = b[i]
                self.basis[i] = self.n + i
            else:
                for j in range(self.n):
                    row[j] = -A[i][j]
                row[self.n + i] = Fraction(-1)
                row[self.n + self.m + art] = Fraction(1)
                row[self.rhs] = -b[i]
                self.basis[i] = self.n + self.m + art
                art += 1

    def _reduced(self, cost):
        reduced = list(cost) + [Fraction(0)] * (self.N - len(cost))
        for i in range(self.m):
            weight = cost[self.basis[i]] if self.basis[i] < len(cost) else Fraction(0)
            if weight:
                row = self.tableau[i]
                for j in range(self.N):
                    reduced[j] -= weight * row[j]
        return reduced

    def _pivot(self, row, column):
        scale = self.tableau[row][column]
        self.tableau[row] = [value / scale for value in self.tableau[row]]
        for i in range(self.m):
            if i != row:
                factor = self.tableau[i][column]
                if factor:
                    target = self.tableau[row]
                    self.tableau[i] = [a - factor * b for a, b in zip(self.tableau[i], target)]
        self.basis[row] = column

    def _run(self, allowed, cost):
        allowed = sorted(allowed)
        while True:
            reduced = self._reduced(cost)
            entering = next((j for j in allowed if reduced[j] > 0), None)
            if entering is None:
                return 'optimal'
            leaving = None
            best = None
            for i in range(self.m):
                coefficient = self.tableau[i][entering]
                if coefficient > 0:
                    ratio = self.tableau[i][self.rhs] / coefficient
                    if best is None or ratio < best or (
                            ratio == best and self.basis[i] < self.basis[leaving]):
                        best = ratio
                        leaving = i
            if leaving is None:
                return 'unbounded'
            self._pivot(leaving, entering)

    def solve(self, objective):
        phase_one = [Fraction(0)] * self.N
        for j in self.artificials:
            phase_one[j] = Fraction(-1)
        if self._run(range(self.N), phase_one) != 'optimal':
            return 'unbounded', None, None
        if any(self.basis[i] in self.artificials and self.tableau[i][self.rhs] != 0
               for i in range(self.m)):
            return 'infeasible', None, None
        # Drive zero-valued artificial basics out so phase two can ignore them.
        for i in range(self.m):
            if self.basis[i] in self.artificials:
                for j in range(self.n + self.m):
                    if self.tableau[i][j] != 0:
                        self._pivot(i, j)
                        break
        phase_two = list(objective) + [Fraction(0)] * (self.N - len(objective))
        state = self._run(range(self.n + self.m), phase_two)
        if state != 'optimal':
            return state, None, None
        value = sum(phase_two[self.basis[i]] * self.tableau[i][self.rhs] for i in range(self.m))
        solution = [Fraction(0)] * self.n
        for i in range(self.m):
            if self.basis[i] < self.n:
                solution[self.basis[i]] = self.tableau[i][self.rhs]
        return 'optimal', value, solution


def linprog(A, b, c):
    """Maximise ``c . x`` subject to ``A x <= b`` and ``x >= 0`` exactly."""
    A = [[_as_fraction(value) for value in row] for row in A]
    b = [_as_fraction(value) for value in b]
    c = [_as_fraction(value) for value in c]
    if A and len(A[0]) != len(c):
        raise ValueError('constraint width does not match objective')
    if len(A) != len(b):
        raise ValueError('constraint height does not match right-hand side')
    width = len(c)
    if not c:
        # No variables: feasibility is exactly whether some b is negative.
        return ('infeasible', None, None) if any(value < 0 for value in b) else ('optimal', Fraction(0), [])
    return _Simplex(A, b, width).solve(c)
