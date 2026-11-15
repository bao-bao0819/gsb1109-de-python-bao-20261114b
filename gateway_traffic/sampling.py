"""Sampling-plan arithmetic and design-based totals for gateway archives.

The gateway samples transactions through two frames:

* the *path frame* selects a transaction with a probability determined by its
  request path;
* the *status frame* selects it with a probability determined by its final
  response status.

A transaction can appear in either archive, or in both.  For a transaction
``i`` let ``p_path_i`` and ``p_status_i`` be the two marginal inclusion
probabilities and ``p_both_i`` their joint inclusion probability.  Its
probability of appearing in the union of the archives is

    pi_i = p_path_i + p_status_i - p_both_i

The Horvitz-Thompson estimate of a class total is ``sum(y_i / pi_i)`` over the
union.  Because different transactions are sampled independently, the
unbiased Horvitz-Thompson variance estimator is

    Var_hat = sum(y_i**2 * (1 - pi_i) / pi_i**2)

summed over transactions in the observed union, with no covariance terms.
For comparison, the true variance of one Bernoulli term is
``y_i**2 * (1 - pi_i) / pi_i``; dividing by ``pi_i`` once more in the
estimator accounts for the fact that only included units are observed.  A
transaction with ``pi_i == 1`` has weight one and contributes exactly zero to
the variance.

All probabilities are kept as :class:`fractions.Fraction` internally so that
certainty units really are certain and do not acquire floating-point error.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

Number = Union[int, float, str, Fraction]


class SamplingError(ValueError):
    """Raised when a sampling plan or archive is internally inconsistent."""


def _fraction(value: Number) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        raise SamplingError("a probability cannot be a bool")
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, float):
        # str() preserves the decimal the user wrote, e.g. 0.1 -> 1/10.
        return Fraction(str(value))
    if isinstance(value, str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise SamplingError(f"invalid probability {value!r}") from exc
    raise SamplingError(f"invalid probability {value!r}")


def _probability(value: Number) -> Fraction:
    prob = _fraction(value)
    if prob < 0 or prob > 1:
        raise SamplingError(f"probability must be in [0, 1], got {value!r}")
    return prob


def _fraction_ratio(value: Number) -> Fraction:
    """Parse a non-negative multiplier such as 0.8 or 2."""

    ratio = _fraction(value)
    if ratio < 0:
        raise SamplingError(f"joint multiplier must be non-negative, got {value!r}")
    return ratio


def _public(value: Optional[Fraction]) -> Optional[Union[int, float]]:
    """Return an int for exact integers, otherwise a float."""

    if value is None:
        return None
    if value.denominator == 1:
        return int(value)
    return float(value)


def _status_matches(pattern: str, status: int) -> bool:
    text = str(pattern).strip()
    if text == "*":
        return True
    if text.startswith("re:"):
        try:
            return re.fullmatch(text[3:], str(status)) is not None
        except re.error as exc:
            raise SamplingError(f"invalid status regex {text!r}: {exc}") from exc
    match = re.fullmatch(r"([1-9])[xX]{2}", text)
    if match:
        return status // 100 == int(match.group(1))
    match = re.fullmatch(r"(\d{3})\s*-\s*(\d{3})", text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return low <= status <= high
    if re.fullmatch(r"\d{3}", text):
        return status == int(text)
    if any(char in text for char in "*?["):
        return fnmatch.fnmatchcase(str(status), text)
    return False


def _path_matches(pattern: str, path: str) -> bool:
    text = str(pattern)
    if text == "*":
        return True
    if text.startswith("re:"):
        try:
            return re.fullmatch(text[3:], path) is not None
        except re.error as exc:
            raise SamplingError(f"invalid path regex {text!r}: {exc}") from exc
    if text.startswith("prefix:"):
        return path.startswith(text[len("prefix:") :])
    if any(char in text for char in "*?["):
        return fnmatch.fnmatchcase(path, text)
    return path == text


def _status_match_example(pattern: str) -> Optional[int]:
    text = str(pattern).strip()
    match = re.fullmatch(r"([1-9])[xX]{2}", text)
    if match:
        return int(match.group(1)) * 100
    match = re.fullmatch(r"(\d{3})\s*-\s*(\d{3})", text)
    if match:
        return int(match.group(1))
    if re.fullmatch(r"\d{3}", text):
        return int(text)
    return None


@dataclass(frozen=True)
class RateRule:
    """One first-match-wins rate rule."""

    match: str
    probability: Fraction
    example: Optional[str] = None

    @classmethod
    def create(cls, match: Any, probability: Number, example: Optional[str] = None) -> "RateRule":
        return cls(str(match), _probability(probability), example)

    def matches(self, value: Union[str, int], kind: str) -> bool:
        if kind == "path":
            return _path_matches(self.match, str(value))
        return _status_matches(self.match, int(value))

    def example_value(self, kind: str) -> Optional[Union[str, int]]:
        if self.example is not None:
            return self.example
        text = str(self.match)
        if kind == "status":
            return _status_match_example(text)
        if text.startswith("re:"):
            return None
        if text.startswith("prefix:"):
            return text[len("prefix:") :] + "__sample__"
        if text == "*":
            return "/__sample__"
        if any(char in text for char in "*?["):
            candidate = text.replace("*", "__sample__").replace("?", "x")
            # A simple class such as [abc] cannot be represented by the
            # literal brackets, so fall back to a generic character.
            if "[" in candidate:
                return None
            return candidate
        return text


@dataclass(frozen=True)
class RateSchedule:
    """A piecewise-constant inclusion-rate schedule."""

    default: Fraction
    rules: Tuple[RateRule, ...]
    kind: str = "path"

    @classmethod
    def create(
        cls,
        default: Number = 0,
        rules: Sequence[Union[RateRule, Mapping[str, Any]]] = (),
        kind: str = "path",
    ) -> "RateSchedule":
        if kind not in ("path", "status"):
            raise SamplingError("kind must be 'path' or 'status'")
        parsed: List[RateRule] = []
        for rule in rules:
            if isinstance(rule, RateRule):
                parsed.append(rule)
            else:
                match = rule.get("match", rule.get("pattern", rule.get("path", rule.get("status"))))
                if match is None:
                    raise SamplingError(f"rate rule is missing a match pattern: {rule!r}")
                if "probability" not in rule and "rate" not in rule:
                    raise SamplingError(f"rate rule is missing a probability: {rule!r}")
                probability = rule.get("probability", rule.get("rate"))
                parsed.append(RateRule.create(match, probability, rule.get("example")))
        return cls(_probability(default), tuple(parsed), kind)

    @classmethod
    def from_spec(cls, spec: Any, kind: str) -> "RateSchedule":
        if isinstance(spec, (int, float, str, Fraction)) and not isinstance(spec, bool):
            return cls.create(spec, (), kind)
        if isinstance(spec, Mapping):
            if "default" in spec or "otherwise" in spec or "rules" in spec:
                default = spec.get("default", spec.get("otherwise", 0))
                return cls.create(default, spec.get("rules", ()), kind)
            # Mapping shorthand: {"path": rate, ...}; unmatched values are
            # unsampled unless "default" is supplied.
            defaults = {"default", "otherwise", "rules"}
            rules = [
                {"match": key, "probability": value}
                for key, value in spec.items()
                if key not in defaults
            ]
            return cls.create(spec.get("default", 0), rules, kind)
        if isinstance(spec, Sequence) and not isinstance(spec, (str, bytes)):
            return cls.create(0, spec, kind)
        raise SamplingError(f"invalid rate schedule: {spec!r}")

    def probability(self, value: Union[str, int]) -> Fraction:
        for rule in self.rules:
            if rule.matches(value, self.kind):
                return rule.probability
        return self.default

    def zero_path_exists(self) -> bool:
        """Whether some request path has probability zero.

        The default region is assumed infinite, so a zero default means zero
        paths exist.  For explicit zero rules we probe a representative path,
        which also catches a zero rule shadowed by an earlier positive rule.
        """

        if self.kind != "path":
            raise SamplingError("zero_path_exists is only meaningful for path schedules")
        if self.default == 0:
            return True
        for rule in self.rules:
            if rule.probability != 0:
                continue
            example = rule.example_value("path")
            if example is None:
                # A regex may or may not match; treating it as a reachable
                # zero region is the conservative choice for estimability.
                return True
            if self.probability(str(example)) == 0:
                return True
        return False

    def zero_statuses(self) -> List[int]:
        if self.kind != "status":
            raise SamplingError("zero_statuses is only meaningful for status schedules")
        return [status for status in range(100, 1000) if self.probability(status) == 0]

    def implied_groups(self, group_by: str) -> List[str]:
        """Status groups explicitly mentioned by rules, for empty reporting."""

        if group_by not in ("status", "status_class"):
            return []
        groups = set()
        for rule in self.rules:
            for status in range(100, 1000):
                if rule.matches(status, "status"):
                    groups.add(_group_key_for_status(status, group_by))
        return sorted(groups)


@dataclass(frozen=True)
class JointRule:
    path_match: Optional[str]
    status_match: Optional[str]
    probability: Fraction

    def matches(self, path: str, status: int) -> bool:
        if self.path_match is not None and not _path_matches(self.path_match, path):
            return False
        if self.status_match is not None and not _status_matches(self.status_match, status):
            return False
        return True


@dataclass(frozen=True)
class JointSchedule:
    """Optional joint-inclusion schedule.

    If ``default`` is None, unmatched transactions use the independent
    product ``p_path * p_status``.  ``relative`` rules instead mean
    ``p_both = factor * p_path * p_status``; this is useful when the two
    routes share some randomness and the joint rate differs by a known factor.
    """

    default: Optional[Fraction] = None
    rules: Tuple[JointRule, ...] = ()
    relative_default: Optional[Fraction] = None
    relative_rules: Tuple[Tuple[Optional[str], Optional[str], Fraction], ...] = ()

    @classmethod
    def from_spec(cls, spec: Any) -> "JointSchedule":
        if isinstance(spec, (int, float, str, Fraction)) and not isinstance(spec, bool):
            return cls(_probability(spec), ())
        if not isinstance(spec, Mapping):
            raise SamplingError(f"invalid joint schedule: {spec!r}")
        if "default" in spec or "otherwise" in spec or "rules" in spec or "relative" in spec:
            default_value = spec.get("default", spec.get("otherwise"))
            default = None if default_value is None else _probability(default_value)
            relative_default_value = spec.get("relative")
            relative_default = (
                None
                if relative_default_value is None
                else _fraction_ratio(relative_default_value)
            )
            rules = []
            for rule in spec.get("rules", ()):
                path_match = rule.get("path", rule.get("path_match"))
                status_match = rule.get("status", rule.get("status_match"))
                if path_match is None and status_match is None:
                    raise SamplingError(f"joint rule needs a path or status match: {rule!r}")
                if "probability" not in rule:
                    raise SamplingError(f"joint rule is missing a probability: {rule!r}")
                rules.append(
                    JointRule(
                        None if path_match is None else str(path_match),
                        None if status_match is None else str(status_match),
                        _probability(rule["probability"]),
                    )
                )
            relative_rules = []
            for rule in spec.get("relative_rules", ()):
                path_match = rule.get("path", rule.get("path_match"))
                status_match = rule.get("status", rule.get("status_match"))
                if path_match is None and status_match is None:
                    raise SamplingError(f"relative joint rule needs a path or status match: {rule!r}")
                if "factor" not in rule:
                    raise SamplingError(f"relative joint rule is missing a factor: {rule!r}")
                relative_rules.append(
                    (
                        None if path_match is None else str(path_match),
                        None if status_match is None else str(status_match),
                        _fraction_ratio(rule["factor"]),
                    )
                )
            return cls(
                default,
                tuple(rules),
                relative_default,
                tuple(relative_rules),
            )
        raise SamplingError(f"invalid joint schedule: {spec!r}")

    def probability(
        self, path: str, status: int, p_path: Fraction, p_status: Fraction
    ) -> Fraction:
        for rule in self.rules:
            if rule.matches(path, status):
                return rule.probability
        for path_match, status_match, factor in self.relative_rules:
            if (
                (path_match is None or _path_matches(path_match, path))
                and (status_match is None or _status_matches(status_match, status))
            ):
                return factor * p_path * p_status
        if self.default is not None:
            return self.default
        if self.relative_default is not None:
            return self.relative_default * p_path * p_status
        return p_path * p_status


@dataclass(frozen=True)
class SamplingPlan:
    """The two-frame sampling design."""

    path: RateSchedule
    status: RateSchedule
    independent_routes: bool = True
    joint: Optional[JointSchedule] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SamplingPlan":
        if not isinstance(data, Mapping):
            raise SamplingError("sampling plan must be a JSON object")
        path_spec = data.get("path", data.get("path_frame", data.get("path_sampling")))
        status_spec = data.get("status", data.get("status_frame", data.get("status_sampling")))
        if path_spec is None and "path_probability" in data:
            path_spec = data["path_probability"]
        if status_spec is None and "status_probability" in data:
            status_spec = data["status_probability"]
        if path_spec is None or status_spec is None:
            raise SamplingError("sampling plan needs both 'path' and 'status' rates")
        joint_spec = data.get("joint", data.get("joint_probability"))
        joint = None if joint_spec is None else JointSchedule.from_spec(joint_spec)
        independent = bool(data.get("independent_routes", data.get("independent", joint is None)))
        return cls(
            path=RateSchedule.from_spec(path_spec, "path"),
            status=RateSchedule.from_spec(status_spec, "status"),
            independent_routes=independent,
            joint=joint,
        )

    @classmethod
    def uniform(
        cls,
        path_probability: Number,
        status_probability: Number,
        *,
        independent_routes: bool = True,
        joint_probability: Optional[Number] = None,
    ) -> "SamplingPlan":
        joint = None if joint_probability is None else JointSchedule.from_spec(joint_probability)
        return cls(
            path=RateSchedule.create(path_probability, (), "path"),
            status=RateSchedule.create(status_probability, (), "status"),
            independent_routes=independent_routes,
            joint=joint,
        )

    def probabilities(
        self, path: str, status: int
    ) -> Tuple[Fraction, Fraction, Fraction, Fraction]:
        """Return ``(p_path, p_status, p_both, pi)`` for one transaction."""

        p_path = self.path.probability(path)
        p_status = self.status.probability(status)
        if self.joint is not None:
            p_both = self.joint.probability(path, status, p_path, p_status)
        elif self.independent_routes:
            p_both = p_path * p_status
        else:
            raise SamplingError(
                "routes are declared dependent but no joint-inclusion schedule was supplied"
            )
        if p_both < 0 or p_both > 1:
            raise SamplingError(f"joint inclusion probability {p_both} is outside [0, 1]")
        if p_both > p_path or p_both > p_status:
            raise SamplingError(
                "joint inclusion probability cannot exceed either marginal probability"
            )
        pi = p_path + p_status - p_both
        if pi > 1:
            raise SamplingError(
                f"union inclusion probability {pi} exceeds 1 for path={path!r}, status={status}"
            )
        return p_path, p_status, p_both, pi

    def group_has_zero_inclusion(self, group: str, group_by: str) -> bool:
        """Whether some transaction in a status group can never enter the union.

        A status is unreachable when the status frame gives it zero probability
        and the path frame does not cover every path (otherwise the path route
        can still catch it).  All 900 status codes are checked exactly, so a
        class is not considered covered just because one status in it has a
        positive rate.
        """

        path_cover_everything = (
            self.path.default == 1 and all(rule.probability == 1 for rule in self.path.rules)
        )
        if group_by == "status":
            statuses = [int(group)]
        elif group_by == "status_class":
            prefix = group[:1]
            statuses = [status for status in range(100, 1000) if str(status).startswith(prefix)]
        else:
            return False
        if path_cover_everything:
            return False
        return any(self.status.probability(status) == 0 for status in statuses)

    def implied_groups(self, group_by: str) -> List[str]:
        return self.status.implied_groups(group_by)


def _group_key_for_status(status: int, group_by: str) -> str:
    if group_by == "status":
        return str(status)
    return f"{status // 100}xx"


def group_key(status: int, group_by: str) -> str:
    if group_by == "status":
        return str(status)
    if group_by == "status_class":
        return _group_key_for_status(status, group_by)
    raise SamplingError(f"unknown group_by {group_by!r}; use 'status' or 'status_class'")


@dataclass(frozen=True)
class ObservedTransaction:
    """A transaction observed in at least one archive (before deduplication)."""

    transaction_id: str
    path: str
    final_status: int
    body_bytes: int
    in_path_archive: bool
    in_status_archive: bool = False
    method: Optional[str] = None
    response_complete: bool = True
    errors: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise SamplingError("transaction_id must not be empty")
        if not 100 <= int(self.final_status) <= 999:
            raise SamplingError(f"invalid final status {self.final_status!r}")
        if self.body_bytes < 0:
            raise SamplingError("body_bytes must not be negative")
        if not self.in_path_archive and not self.in_status_archive:
            raise SamplingError("transaction is not present in either archive")

    @property
    def status_class(self) -> str:
        return group_key(self.final_status, "status_class")


@dataclass(frozen=True)
class TransactionEstimate:
    transaction_id: str
    path: str
    method: Optional[str]
    final_status: int
    status_class: str
    body_bytes: int
    in_path_archive: bool
    in_status_archive: bool
    p_path: float
    p_status: float
    p_both: float
    p_inclusion: float
    weight: float
    contribution_bytes: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "path": self.path,
            "method": self.method,
            "final_status": self.final_status,
            "status_class": self.status_class,
            "body_bytes": self.body_bytes,
            "in_path_archive": self.in_path_archive,
            "in_status_archive": self.in_status_archive,
            "p_path": self.p_path,
            "p_status": self.p_status,
            "p_both": self.p_both,
            "p_inclusion": self.p_inclusion,
            "weight": self.weight,
            "contribution_bytes": self.contribution_bytes,
        }


@dataclass(frozen=True)
class ClassEstimate:
    group: str
    observed_transactions: int
    certainty_transactions: int
    estimated_bytes: Optional[Union[int, float]]
    variance: Optional[Union[int, float]]
    standard_error: Optional[float]
    estimable: bool
    reason: Optional[str]
    zero_inclusion: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "observed_transactions": self.observed_transactions,
            "certainty_transactions": self.certainty_transactions,
            "estimated_bytes": self.estimated_bytes,
            "variance": self.variance,
            "standard_error": self.standard_error,
            "estimable": self.estimable,
            "reason": self.reason,
            "zero_inclusion": self.zero_inclusion,
        }


@dataclass(frozen=True)
class EstimateReport:
    metric: str
    group_by: str
    classes: Tuple[ClassEstimate, ...]
    overall: ClassEstimate
    transactions: Tuple[TransactionEstimate, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "group_by": self.group_by,
            "classes": [item.as_dict() for item in self.classes],
            "overall": self.overall.as_dict(),
            "transactions": [item.as_dict() for item in self.transactions],
        }


def _summarize(
    group: str,
    rows: Sequence[Tuple[ObservedTransaction, Fraction, Fraction, Fraction, Fraction]],
    *,
    zero_inclusion: bool,
    empty_reason: str,
) -> ClassEstimate:
    observed = len(rows)
    certainty = sum(1 for _, _, _, _, pi in rows if pi == 1)
    if not rows:
        reason = (
            "some transactions in this class have zero inclusion probability; "
            "the class total is not estimable"
            if zero_inclusion
            else empty_reason
        )
        return ClassEstimate(
            group=group,
            observed_transactions=0,
            certainty_transactions=0,
            estimated_bytes=None,
            variance=None,
            standard_error=None,
            estimable=False,
            reason=reason,
            zero_inclusion=zero_inclusion,
        )
    if any(pi == 0 for _, _, _, _, pi in rows):
        return ClassEstimate(
            group=group,
            observed_transactions=observed,
            certainty_transactions=certainty,
            estimated_bytes=None,
            variance=None,
            standard_error=None,
            estimable=False,
            reason=(
                "an observed transaction has zero union inclusion probability; "
                "check that the archive membership matches the sampling plan"
            ),
            zero_inclusion=True,
        )
    if zero_inclusion:
        return ClassEstimate(
            group=group,
            observed_transactions=observed,
            certainty_transactions=certainty,
            estimated_bytes=None,
            variance=None,
            standard_error=None,
            estimable=False,
            reason=(
                "some transactions in this class have zero inclusion probability; "
                "the class total is not estimable"
            ),
            zero_inclusion=True,
        )

    estimate = Fraction(0)
    variance = Fraction(0)
    for transaction, _, _, _, pi in rows:
        y = Fraction(transaction.body_bytes, 1)
        estimate += y / pi
        # Unbiased HT variance estimator.  The true variance of this unit's
        # contribution is y^2 (1-pi)/pi, but the estimator observes a unit
        # only with probability pi, so the HT variance estimate divides by pi
        # once more.  For pi = 1 the term is exactly zero.
        variance += y * y * (1 - pi) / (pi * pi)
    standard_error = None
    if variance >= 0:
        standard_error = float(variance) ** 0.5
    return ClassEstimate(
        group=group,
        observed_transactions=observed,
        certainty_transactions=certainty,
        estimated_bytes=_public(estimate),
        variance=_public(variance),
        standard_error=standard_error,
        estimable=True,
        reason=None,
        zero_inclusion=False,
    )


def estimate(
    transactions: Iterable[ObservedTransaction],
    plan: SamplingPlan,
    *,
    group_by: str = "status_class",
    known_groups: Optional[Sequence[str]] = None,
    include_empty_classes: bool = True,
    metric: str = "response body bytes (h11 Data events)",
) -> EstimateReport:
    """Estimate class totals and variances from the deduplicated union.

    ``transactions`` must contain each transaction once, with flags saying
    which archives contained it.  If the same transaction is supplied twice,
    this function raises rather than double-counting it.

    With ``include_empty_classes`` (the default), the five standard HTTP
    status classes are always reported.  A class with no sampled transaction
    is explicitly not estimable: an empty archive is not evidence of zero
    traffic.
    """

    if group_by not in ("status", "status_class"):
        raise SamplingError("group_by must be 'status' or 'status_class'")

    transaction_list = list(transactions)
    seen: Dict[str, ObservedTransaction] = {}
    for transaction in transaction_list:
        if transaction.transaction_id in seen:
            raise SamplingError(
                f"duplicate transaction id {transaction.transaction_id!r}; "
                "deduplicate archives before estimating"
            )
        seen[transaction.transaction_id] = transaction

    rows: List[Tuple[ObservedTransaction, Fraction, Fraction, Fraction, Fraction]] = []
    tx_rows: List[TransactionEstimate] = []
    groups = set(known_groups or ())
    groups.update(plan.implied_groups(group_by))
    if include_empty_classes and group_by == "status_class":
        groups.update({"1xx", "2xx", "3xx", "4xx", "5xx"})

    for transaction in transaction_list:
        p_path, p_status, p_both, pi = plan.probabilities(
            transaction.path, transaction.final_status
        )
        if pi == 0:
            # Keep the row so the class can explain why it is inestimable.
            rows.append((transaction, p_path, p_status, p_both, pi))
            groups.add(group_key(transaction.final_status, group_by))
            tx_rows.append(
                TransactionEstimate(
                    transaction_id=transaction.transaction_id,
                    path=transaction.path,
                    method=transaction.method,
                    final_status=transaction.final_status,
                    status_class=transaction.status_class,
                    body_bytes=transaction.body_bytes,
                    in_path_archive=transaction.in_path_archive,
                    in_status_archive=transaction.in_status_archive,
                    p_path=float(p_path),
                    p_status=float(p_status),
                    p_both=float(p_both),
                    p_inclusion=0.0,
                    weight=float("inf"),
                    contribution_bytes=float("inf"),
                )
            )
            continue
        weight = Fraction(1, 1) / pi
        contribution = Fraction(transaction.body_bytes, 1) / pi
        groups.add(group_key(transaction.final_status, group_by))
        rows.append((transaction, p_path, p_status, p_both, pi))
        tx_rows.append(
            TransactionEstimate(
                transaction_id=transaction.transaction_id,
                path=transaction.path,
                method=transaction.method,
                final_status=transaction.final_status,
                status_class=transaction.status_class,
                body_bytes=transaction.body_bytes,
                in_path_archive=transaction.in_path_archive,
                in_status_archive=transaction.in_status_archive,
                p_path=float(p_path),
                p_status=float(p_status),
                p_both=float(p_both),
                p_inclusion=float(pi),
                weight=float(weight),
                contribution_bytes=float(contribution),
            )
        )

    classes: List[ClassEstimate] = []
    for group in sorted(groups):
        group_rows = [
            row for row in rows if group_key(row[0].final_status, group_by) == group
        ]
        classes.append(
            _summarize(
                group,
                group_rows,
                zero_inclusion=plan.group_has_zero_inclusion(group, group_by),
                empty_reason=(
                    "no transactions were sampled into this class; an empty archive "
                    "is not evidence of zero traffic, so the class total is not estimable"
                ),
            )
        )

    if not transaction_list:
        overall = ClassEstimate(
            group="overall",
            observed_transactions=0,
            certainty_transactions=0,
            estimated_bytes=None,
            variance=None,
            standard_error=None,
            estimable=False,
            reason=(
                "no transactions were observed; empty archives are not zero traffic, "
                "so the total is not estimable"
            ),
            zero_inclusion=False,
        )
    elif any(
        item.zero_inclusion or any(pi == 0 for _, _, _, _, pi in rows)
        for item in classes
    ):
        reasons = [item.group for item in classes if item.zero_inclusion]
        overall = ClassEstimate(
            group="overall",
            observed_transactions=len(transaction_list),
            certainty_transactions=sum(
                1 for _, _, _, _, pi in rows if pi == 1
            ),
            estimated_bytes=None,
            variance=None,
            standard_error=None,
            estimable=False,
            reason=(
                "not estimable because some transactions have zero inclusion "
                f"probability in these classes: {', '.join(reasons)}"
            ),
            zero_inclusion=True,
        )
    else:
        # Every observed transaction has positive inclusion probability, so
        # the union-wide HT total is estimable even if some class happened to
        # receive no sample.  Its variance only reflects sampled units; the
        # per-class report flags classes with no coverage.
        overall = _summarize(
            "overall",
            rows,
            zero_inclusion=False,
            empty_reason="no transactions were observed",
        )

    return EstimateReport(
        metric=metric,
        group_by=group_by,
        classes=tuple(classes),
        overall=overall,
        transactions=tuple(tx_rows),
    )
