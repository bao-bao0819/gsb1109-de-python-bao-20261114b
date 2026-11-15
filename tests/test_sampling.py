import math

import pytest

from gateway_traffic.sampling import (
    ObservedTransaction,
    SamplingError,
    SamplingPlan,
    estimate,
)


def test_independent_routes_joint_and_union_probability():
    plan = SamplingPlan.uniform("1/5", "1/2")
    p_path, p_status, p_both, pi = plan.probabilities("/x", 200)
    assert p_path == pytest.approx(0.2)
    assert p_status == pytest.approx(0.5)
    assert p_both == pytest.approx(0.1)
    assert pi == pytest.approx(0.6)


def test_ht_total_and_variance_estimate_by_class():
    plan = SamplingPlan.uniform("1/2", "1/2")
    transactions = [
        ObservedTransaction("a", "/a", 200, 10, True, True),
        ObservedTransaction("b", "/b", 200, 20, True, False),
        ObservedTransaction("c", "/c", 404, 5, False, True),
    ]
    report = estimate(transactions, plan)
    two_xx = next(item for item in report.classes if item.group == "2xx")
    four_xx = next(item for item in report.classes if item.group == "4xx")

    # pi = 0.5 + 0.5 - 0.25 = 0.75 for every observed transaction.
    assert two_xx.estimated_bytes == pytest.approx((10 + 20) / 0.75)
    assert two_xx.variance == pytest.approx(
        (10**2 + 20**2) * (1 - 0.75) / 0.75**2
    )
    assert four_xx.estimated_bytes == pytest.approx(5 / 0.75)
    assert four_xx.variance == pytest.approx(5**2 * (1 - 0.75) / 0.75**2)
    assert report.overall.estimated_bytes == pytest.approx((10 + 20 + 5) / 0.75)
    assert report.overall.variance == pytest.approx(
        (10**2 + 20**2 + 5**2) * (1 - 0.75) / 0.75**2
    )


def test_certainty_transaction_is_counted_once_with_zero_variance():
    plan = SamplingPlan.uniform(1, 1)
    transactions = [ObservedTransaction("must", "/health", 200, 1234, True, True)]
    report = estimate(transactions, plan)
    two_xx = next(item for item in report.classes if item.group == "2xx")
    assert two_xx.observed_transactions == 1
    assert two_xx.certainty_transactions == 1
    assert two_xx.estimated_bytes == 1234
    assert two_xx.variance == 0
    assert two_xx.standard_error == 0
    row = report.transactions[0]
    assert row.p_inclusion == 1
    assert row.weight == 1
    assert row.contribution_bytes == 1234


def test_certainty_via_union_of_two_zero_marginals_is_still_zero_variance():
    # Each route can miss the transaction, but the union is certain.
    plan = SamplingPlan.uniform(1, "1/2")
    transactions = [ObservedTransaction("both", "/x", 200, 100, True, True)]
    report = estimate(transactions, plan)
    row = report.transactions[0]
    assert row.p_path == 1
    assert row.p_status == pytest.approx(0.5)
    assert row.p_both == pytest.approx(0.5)
    assert row.p_inclusion == 1
    two_xx = next(item for item in report.classes if item.group == "2xx")
    assert two_xx.estimated_bytes == 100
    assert two_xx.variance == 0


def test_zero_inclusion_class_is_not_estimable_not_zero():
    # The path route can never catch anything, and 4xx is not in the status
    # frame either, so every 4xx transaction has pi = 0.
    plan = SamplingPlan(
        path=type(SamplingPlan.uniform(0, 0).path).create(0, [], "path"),
        status=type(SamplingPlan.uniform(0, 0).status).create(
            1, [{"match": "4xx", "probability": 0}], "status"
        ),
    )
    report = estimate([ObservedTransaction("a", "/a", 200, 10, False, True)], plan)
    four_xx = next(item for item in report.classes if item.group == "4xx")
    assert four_xx.estimable is False
    assert four_xx.estimated_bytes is None
    assert "zero inclusion" in four_xx.reason
    two_xx = next(item for item in report.classes if item.group == "2xx")
    assert two_xx.estimable is True
    assert two_xx.estimated_bytes == 10
    assert report.overall.estimable is False
    assert report.overall.estimated_bytes is None


def test_empty_sampled_class_is_not_estimable():
    plan = SamplingPlan.uniform("1/2", "1/2")
    report = estimate([ObservedTransaction("a", "/a", 200, 10, True, True)], plan)
    three_xx = next(item for item in report.classes if item.group == "3xx")
    assert three_xx.estimable is False
    assert three_xx.estimated_bytes is None
    assert "empty archive" in three_xx.reason


def test_empty_archives_are_not_zero_traffic():
    plan = SamplingPlan.uniform("1/2", "1/2")
    report = estimate([], plan)
    assert report.overall.estimable is False
    assert report.overall.estimated_bytes is None
    assert "not zero traffic" in report.overall.reason


def test_duplicate_identity_is_rejected():
    plan = SamplingPlan.uniform("1/2", "1/2")
    transactions = [
        ObservedTransaction("same", "/a", 200, 10, True, False),
        ObservedTransaction("same", "/a", 200, 10, False, True),
    ]
    with pytest.raises(SamplingError, match="duplicate transaction id"):
        estimate(transactions, plan)


def test_joint_probability_override():
    plan = SamplingPlan.uniform("1/2", "1/2", joint_probability=0)
    _, _, p_both, pi = plan.probabilities("/x", 200)
    assert p_both == 0
    assert pi == 1


def test_dependent_routes_require_a_joint_schedule():
    plan = SamplingPlan(
        path=SamplingPlan.uniform("1/2", "1/2").path,
        status=SamplingPlan.uniform("1/2", "1/2").status,
        independent_routes=False,
    )
    with pytest.raises(SamplingError, match="joint-inclusion"):
        plan.probabilities("/x", 200)


def test_variance_is_zero_when_all_weights_are_one():
    plan = SamplingPlan.uniform(1, 1)
    transactions = [
        ObservedTransaction("a", "/a", 200, 10, True, True),
        ObservedTransaction("b", "/b", 200, 20, True, True),
    ]
    report = estimate(transactions, plan)
    assert report.overall.estimated_bytes == 30
    assert report.overall.variance == 0


def test_variance_estimate_is_unbiased_over_membership_outcomes():
    """Enumerate the four union outcomes rather than trusting the formula."""

    plan = SamplingPlan.uniform("1/3", "1/2")
    p_path, p_status, p_both, pi = plan.probabilities("/x", 200)
    probabilities = {
        "none": (1 - p_path) * (1 - p_status),
        "path": p_path * (1 - p_status),
        "status": (1 - p_path) * p_status,
        "both": p_both,
    }
    assert sum(probabilities.values()) == 1
    y = 12
    outcomes = {
        key: (0 if key == "none" else y / float(pi))
        for key in probabilities
    }
    expected_estimate = sum(
        probability * outcomes[key] for key, probability in probabilities.items()
    )
    true_variance = sum(
        probability * (outcomes[key] - expected_estimate) ** 2
        for key, probability in probabilities.items()
    )
    expected_variance_estimate = sum(
        probability * (0 if key == "none" else y**2 * (1 - pi) / pi**2)
        for key, probability in probabilities.items()
    )
    assert expected_estimate == pytest.approx(y)
    assert expected_variance_estimate == pytest.approx(true_variance)
