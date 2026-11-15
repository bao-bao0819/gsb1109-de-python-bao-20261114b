# -*- coding: utf-8 -*-
from fractions import Fraction

import pytest

from canmatrix._milp import UnboundedLP
from canmatrix._milp import feasible_point, maximize, minimize, solve_lp


def test_lp_solves_a_simple_system():
    value, point = solve_lp([[1, 1], [1, -1]], [10, 2], [1, 2])
    assert value == 14
    assert point == [Fraction(6), Fraction(4)]


def test_integer_maximum_differs_from_lp_relaxation():
    value, point = maximize([1, 1], [[5, 7]], [100])
    assert value == 20
    assert sum(point) == 20


def test_inequalities_are_applied_together_with_equalities():
    value, point = maximize([1, 1], [], [], [([1, 0], ">=", 2), ([0, 1], ">=", 3),
                                            ([1, 1], "<=", 7)])
    assert value == 7
    assert 2 <= point[0] <= 4
    assert 3 <= point[1] <= 5


def test_infeasible_system_returns_none():
    assert solve_lp([], [], [1], [([1], ">=", 5), ([1], "<=", 3)]) is None
    assert feasible_point([], [], [([1], ">=", 5), ([1], "<=", 3)]) is None
    assert maximize([0, 0], [[1, 1]], [3], [([1, 0], ">=", 5)]) is None


def test_unbounded_objective_is_reported():
    with pytest.raises(UnboundedLP):
        solve_lp([[0, 1]], [4], [1, 1])


def test_minimize_negates_the_objective():
    value, point = minimize([1, 0], [[1, 1]], [10])
    assert value == 0
    assert point[0] == 0


def test_non_negative_variables_are_enforced():
    assert maximize([1], [], [], [([1], ">=", -5), ([1], "<=", 4)])[0] == 4


def test_integral_but_non_divisible_equality_is_infeasible():
    assert maximize([1], [[3]], [10]) is None


def test_zero_variable_model():
    assert feasible_point([], [], []) == []
