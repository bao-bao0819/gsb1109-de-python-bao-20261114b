# -*- coding: utf-8 -*-
"""Exact linear/integer optimisation for the status reconciliation model.

The reconciliation model only needs non-negative integer variables, linear
equalities, a few linear inequalities and linear objectives.  A full MILP
dependency would be disproportionate (and is not part of canmatrix's base
install), so this module implements that small subset with exact rational
arithmetic:

* a two-phase simplex solver for the LP relaxation,
* branch-and-bound for integrality.

The solver is deliberately private and deterministic (Bland's rule).  It is
not a general-purpose MILP replacement; it exists to make
:mod:`canmatrix.reconcile` independent of optional third-party packages.
"""

from fractions import Fraction


class UnboundedLP(RuntimeError):
    """The LP relaxation has no finite optimum."""


class BranchLimit(RuntimeError):
    """The branch-and-bound search exceeded its node budget."""


def _fraction(value):
    # type: (object) -> Fraction
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(value)


def _canonical_objective(rows, basis, coefficients):
    # type: (list, list, list) -> list
    objective = list(coefficients) + [Fraction(0)]
    for row_index, basic in enumerate(basis):
        factor = coefficients[basic]
        if factor != 0:
            row = rows[row_index]
            for column in range(len(objective)):
                objective[column] -= factor * row[column]
    return objective


def _pivot(rows, objective, basis, row_index, column):
    # type: (list, list, list, int, int) -> None
    pivot_row = rows[row_index]
    pivot = pivot_row[column]
    if pivot == 0:
        raise ArithmeticError("zero pivot")
    pivot_row = [value / pivot for value in pivot_row]
    rows[row_index] = pivot_row

    for index, row in enumerate(rows):
        if index == row_index:
            continue
        factor = row[column]
        if factor != 0:
            rows[index] = [left - factor * right
                           for left, right in zip(row, pivot_row)]

    factor = objective[column]
    if factor != 0:
        for index in range(len(objective)):
            objective[index] -= factor * pivot_row[index]
    objective[column] = Fraction(0)
    basis[row_index] = column


def _run_simplex(rows, objective, basis, entering_allowed, max_iterations=50000):
    # type: (list, list, list, list, int) -> None
    rows_count = len(rows)
    columns = len(objective) - 1

    for _ in range(max_iterations):
        entering = None
        for column in range(columns):
            if entering_allowed[column] and objective[column] > 0:
                entering = column
                break
        if entering is None:
            return

        leaving = None
        best_ratio = None
        for row_index in range(rows_count):
            coefficient = rows[row_index][entering]
            if coefficient <= 0:
                continue
            ratio = rows[row_index][-1] / coefficient
            if (best_ratio is None or ratio < best_ratio or
                    (ratio == best_ratio and basis[row_index] < basis[leaving])):
                best_ratio = ratio
                leaving = row_index
        if leaving is None:
            raise UnboundedLP("LP relaxation is unbounded")
        _pivot(rows, objective, basis, leaving, entering)

    raise ArithmeticError("simplex iteration limit exceeded")


def solve_lp(equalities, right_hand_sides, objective, inequalities=()):
    # type: (list, list, list, object) -> object
    """Solve ``max objective * x`` over the LP relaxation.

    ``equalities`` is a matrix (sequence of coefficient sequences),
    ``right_hand_sides`` the matching right-hand side, and ``inequalities`` a
    sequence of ``(coefficients, sense, rhs)`` with ``sense`` one of ``<=``,
    ``>=`` or ``=``.

    Returns ``(objective_value, values)`` for the original variables, or
    ``None`` when the relaxation is infeasible.  Values are exact
    :class:`fractions.Fraction` instances.
    """
    original_count = len(objective)
    specs = []

    for coefficients, rhs in zip(equalities, right_hand_sides):
        specs.append((list(coefficients), "=", _fraction(rhs)))
    for coefficients, sense, rhs in inequalities:
        specs.append((list(coefficients), sense, _fraction(rhs)))

    normalised = []
    for coefficients, sense, rhs in specs:
        if len(coefficients) < original_count:
            coefficients = coefficients + [0] * (original_count - len(coefficients))
        if len(coefficients) > original_count:
            raise ValueError("constraint has too many coefficients")
        coefficients = [_fraction(value) for value in coefficients]
        if rhs < 0:
            coefficients = [-value for value in coefficients]
            rhs = -rhs
            if sense == "<=":
                sense = ">="
            elif sense == ">=":
                sense = "<="
        normalised.append((coefficients, sense, rhs))

    inequality_count = sum(1 for _, sense, _ in normalised if sense != "=")
    non_artificial_count = original_count + inequality_count
    slack_column = original_count
    slack_columns = []
    for _, sense, _ in normalised:
        if sense == "=":
            slack_columns.append(None)
        else:
            slack_columns.append(slack_column)
            slack_column += 1

    rows_count = len(normalised)
    total_columns = non_artificial_count + rows_count
    rows = []
    for row_index, (coefficients, sense, rhs) in enumerate(normalised):
        row = [Fraction(0)] * (total_columns + 1)
        row[:original_count] = coefficients
        if sense == "<=":
            row[slack_columns[row_index]] = Fraction(1)
        elif sense == ">=":
            row[slack_columns[row_index]] = Fraction(-1)
        row[-1] = rhs
        rows.append(row)

    artificial_columns = [non_artificial_count + index for index in range(rows_count)]
    for row_index, artificial in enumerate(artificial_columns):
        rows[row_index][artificial] = Fraction(1)

    basis = list(artificial_columns)

    # Phase I: maximise -sum(artificial variables).
    phase_one = [Fraction(0)] * total_columns
    for column in artificial_columns:
        phase_one[column] = Fraction(-1)
    objective_row = _canonical_objective(rows, basis, phase_one)
    _run_simplex(rows, objective_row, basis, [True] * total_columns)
    if -objective_row[-1] < 0:
        return None

    # Phase II: the original objective; artificial variables may not enter.
    phase_two = [Fraction(0)] * total_columns
    for column, value in enumerate(objective):
        phase_two[column] = _fraction(value)
    objective_row = _canonical_objective(rows, basis, phase_two)
    entering_allowed = [True] * total_columns
    for column in artificial_columns:
        entering_allowed[column] = False
    _run_simplex(rows, objective_row, basis, entering_allowed)

    values = [Fraction(0)] * total_columns
    for row_index, basic in enumerate(basis):
        value = rows[row_index][-1]
        if value < 0:
            return None
        values[basic] = value
    original_values = values[:original_count]
    objective_value = sum(_fraction(coefficient) * value
                          for coefficient, value in zip(objective, original_values))
    return objective_value, original_values


def maximize(objective, equalities, right_hand_sides, inequalities=(),
             node_limit=100000):
    # type: (list, list, list, object, int) -> object
    """Maximise an integer-linear objective.

    Returns ``(objective_value, values)`` or ``None`` when no integer solution
    exists.  Raises :class:`UnboundedLP` when the objective has no finite
    maximum and :class:`BranchLimit` when the node budget is exhausted.
    """
    objective = [_fraction(value) for value in objective]
    variable_count = len(objective)
    base_inequalities = list(inequalities)
    best_value = None
    best_values = None
    nodes = 0

    def search(extra_inequalities):
        # type: (list) -> None
        nonlocal best_value, best_values, nodes
        nodes += 1
        if nodes > node_limit:
            raise BranchLimit("integer search exceeded %d nodes" % node_limit)

        relaxed = solve_lp(equalities, right_hand_sides, objective,
                           base_inequalities + extra_inequalities)
        if relaxed is None:
            return
        relaxed_value, values = relaxed
        if best_value is not None and relaxed_value <= best_value:
            return

        fractional = None
        for index, value in enumerate(values):
            if value.denominator != 1:
                fractional = index
                break
        if fractional is None:
            best_value = relaxed_value
            best_values = values
            return

        value = values[fractional]
        floor_value = value.numerator // value.denominator
        ceil_value = floor_value + 1
        less_row = ([Fraction(1) if index == fractional else Fraction(0)
                     for index in range(variable_count)],
                    "<=", Fraction(floor_value))
        greater_row = ([Fraction(1) if index == fractional else Fraction(0)
                        for index in range(variable_count)],
                       ">=", Fraction(ceil_value))

        if objective[fractional] >= 0:
            branches = (greater_row, less_row)
        else:
            branches = (less_row, greater_row)
        for branch in branches:
            search(extra_inequalities + [branch])

    search([])
    if best_values is None:
        return None
    return best_value, best_values


def minimize(objective, equalities, right_hand_sides, inequalities=(),
             node_limit=100000):
    # type: (list, list, list, object, int) -> object
    """Minimise an integer-linear objective."""
    negated = [-_fraction(value) for value in objective]
    result = maximize(negated, equalities, right_hand_sides, inequalities, node_limit)
    if result is None:
        return None
    value, values = result
    return -value, values


def feasible_point(equalities, right_hand_sides, inequalities=(), node_limit=100000):
    # type: (list, list, object, int) -> object
    """Return an integer feasible point, or ``None``."""
    if equalities:
        variable_count = len(equalities[0])
    elif inequalities:
        variable_count = len(next(iter(inequalities))[0])
    else:
        variable_count = 0
    result = maximize([0] * variable_count, equalities, right_hand_sides,
                      inequalities, node_limit)
    if result is None:
        return None
    return result[1]


__all__ = [
    "BranchLimit",
    "UnboundedLP",
    "feasible_point",
    "maximize",
    "minimize",
    "solve_lp",
]
