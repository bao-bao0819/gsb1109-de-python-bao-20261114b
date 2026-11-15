"""
Configurable inheritance semantics for testsuite properties.

In a batch of JUnit XML reports, a property such as ``region`` may be
defined on a top-level testsuite and redefined by nested suites. This
module resolves, for every testcase, the effective value of each property
according to a per-attribute configuration:

- ``NEAREST``: the definition closest to the testcase wins.
- ``ACCUMULATE``: all definitions along the path are collected into a set.
- ``UNIFORM``: definitions along the whole path must agree.

Resolution is purely path-based: same-named sibling suites are interpreted
independently, each testcase resolving through its own ancestor chain. If a
single-valued attribute (``NEAREST`` or ``UNIFORM``) is given two different
values at the same level, or a ``UNIFORM`` attribute disagrees along the
path, the conflicting sources are listed and the affected cases are
quarantined.

Every resolved dimension keeps its effective sources, so when an
upper-level property is changed later, :meth:`InheritanceResult.impacted_cases`
tells which historical cases' dimensions would actually change with it.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, Iterator, List, Tuple

from .junitparser import JUnitXml, TestCase, TestSuite


class InheritanceMode(Enum):
    """How a property defined on nested testsuites is inherited."""

    NEAREST = "nearest"
    ACCUMULATE = "accumulate"
    UNIFORM = "uniform"


class InheritanceConfig:
    """Maps property names to their inheritance modes.

    Attributes:
        modes: Mapping of property name to :class:`InheritanceMode`.
        default: Mode used for properties not present in ``modes``.
    """

    def __init__(
        self,
        modes: Dict[str, InheritanceMode | str] | None = None,
        default: InheritanceMode | str = InheritanceMode.NEAREST,
    ):
        self.default = InheritanceMode(default)
        self.modes: Dict[str, InheritanceMode] = {}
        for name, mode in (modes or {}).items():
            self.modes[name] = InheritanceMode(mode)

    def mode_for(self, name: str) -> InheritanceMode:
        """Return the configured inheritance mode for property *name*."""
        return self.modes.get(name, self.default)

    @classmethod
    def from_dict(cls, data: Dict) -> "InheritanceConfig":
        """Build a config from a dict such as
        ``{"region": "nearest", "labels": "accumulate", "default": "nearest"}``.
        """
        data = dict(data)
        default = data.pop("default", InheritanceMode.NEAREST)
        return cls(modes=data, default=default)

    @classmethod
    def from_json(cls, file) -> "InheritanceConfig":
        """Build a config from a JSON file path or file object."""
        if hasattr(file, "read"):
            data = json.load(file)
        else:
            with open(file, encoding="utf-8") as fp:
                data = json.load(fp)
        return cls.from_dict(data)


@dataclass(frozen=True)
class PropertySource:
    """One concrete ``<property>`` definition on a testsuite.

    Attributes:
        path: Suite names from the top-level suite down to the defining
            suite. Same-named siblings are disambiguated with a ``[i]``
            suffix (1-based occurrence index).
        name: Property name.
        value: Property value.
    """

    path: Tuple[str, ...]
    name: str
    value: str

    def __str__(self):
        return f"{'/'.join(self.path)}: {self.name}={self.value}"


@dataclass(frozen=True)
class ResolvedDimension:
    """The effective value of one property for one testcase.

    Attributes:
        name: Property name.
        mode: The inheritance mode used.
        value: A string for ``NEAREST``/``UNIFORM``, a ``frozenset`` of
            strings for ``ACCUMULATE``.
        sources: The definitions that effectively contribute to ``value``.
            For ``NEAREST`` this is only the nearest definition, so changing
            a shadowed upper-level definition does not affect this case.
    """

    name: str
    mode: InheritanceMode
    value: "str | frozenset"
    sources: Tuple[PropertySource, ...]


@dataclass
class PropertyConflict:
    """An unresolvable single-valued attribute.

    Attributes:
        name: Property name.
        mode: The inheritance mode that was violated.
        sources: All conflicting definitions, for tracing the origin of
            each disagreeing value.
        cases: The testcases quarantined by this conflict.
    """

    name: str
    mode: InheritanceMode
    sources: Tuple[PropertySource, ...]
    cases: List[TestCase] = field(default_factory=list)

    def __str__(self):
        origins = "; ".join(str(source) for source in self.sources)
        return f"{self.name} ({self.mode.value}): conflicting values from {origins}"


@dataclass
class CaseDimensions:
    """Resolved property dimensions for one testcase.

    Attributes:
        case: The testcase.
        dimensions: Mapping of property name to :class:`ResolvedDimension`.
            Conflicted attributes are absent.
        conflicts: Conflicts that caused this case to be quarantined.
    """

    case: TestCase
    dimensions: Dict[str, ResolvedDimension]
    conflicts: List[PropertyConflict] = field(default_factory=list)

    @property
    def quarantined(self) -> bool:
        """Whether this case was quarantined due to conflicts."""
        return bool(self.conflicts)

    def __getitem__(self, name: str) -> ResolvedDimension:
        return self.dimensions[name]

    def get(self, name: str, default=None):
        return self.dimensions.get(name, default)


class InheritanceResult:
    """Outcome of resolving property inheritance over a JUnit XML tree."""

    def __init__(
        self,
        cases: List[CaseDimensions],
        definitions: List[PropertySource],
    ):
        self.cases = cases
        self.definitions = definitions

    @property
    def resolved(self) -> List[CaseDimensions]:
        """Cases whose dimensions were fully resolved."""
        return [cd for cd in self.cases if not cd.quarantined]

    @property
    def quarantined(self) -> List[CaseDimensions]:
        """Cases isolated because at least one attribute conflicted."""
        return [cd for cd in self.cases if cd.quarantined]

    @property
    def conflicts(self) -> List[PropertyConflict]:
        """All conflicts, each listing its sources and affected cases."""
        seen = {}
        ordered = []
        for cd in self.cases:
            for conflict in cd.conflicts:
                key = id(conflict)
                if key not in seen:
                    seen[key] = conflict
                    ordered.append(conflict)
        return ordered

    def impacted_cases(self, source: PropertySource) -> List[TestCase]:
        """Cases whose resolved dimensions would change if the definition
        *source* were changed.

        Only effective sources count: for ``NEAREST`` attributes, cases
        shadowed by a closer definition are not affected by an upper-level
        change. Conflicted (unresolvable) dimensions are not tracked.
        """
        impacted = []
        for cd in self.cases:
            if any(source in dim.sources for dim in cd.dimensions.values()):
                impacted.append(cd.case)
        return impacted

    def impacted_by(self, path: Iterable[str] | str, name: str) -> List[TestCase]:
        """Convenience wrapper around :meth:`impacted_cases`.

        *path* identifies the defining suite, either as a tuple of suite
        names from the top level down (as in :attr:`PropertySource.path`)
        or as a single top-level suite name. All definitions matching
        ``(path, name)`` are considered.
        """
        if isinstance(path, str):
            path = (path,)
        path = tuple(path)
        impacted = []
        for source in self.definitions:
            if source.path == path and source.name == name:
                for case in self.impacted_cases(source):
                    if case not in impacted:
                        impacted.append(case)
        return impacted


def _sibling_entries(suites: List[TestSuite]) -> Iterator[Tuple[str, TestSuite]]:
    """Yield ``(path_entry, suite)`` pairs, disambiguating same-named siblings."""
    names = [suite.name or "" for suite in suites]
    counts = Counter(names)
    seen = Counter()
    for suite, name in zip(suites, names):
        seen[name] += 1
        entry = name if counts[name] == 1 else f"{name}[{seen[name]}]"
        yield entry, suite


class _Resolver:
    def __init__(self, config: InheritanceConfig):
        self.config = config
        self.cases: List[CaseDimensions] = []
        self.definitions: List[PropertySource] = []
        self._conflicts: Dict[Tuple, PropertyConflict] = {}

    def walk(self, suite: TestSuite, path: Tuple[str, ...], chain: List[Dict]):
        own: Dict[str, List[PropertySource]] = {}
        for prop in suite.properties():
            source = PropertySource(path=path, name=prop.name, value=prop.value)
            own.setdefault(prop.name, []).append(source)
            self.definitions.append(source)
        chain = chain + [own]
        for case in suite.iterchildren(suite.testcase):
            self.resolve_case(case, chain)
        for entry, child in _sibling_entries(list(suite.testsuites())):
            self.walk(child, path + (entry,), chain)

    def _conflict(
        self, name: str, mode: InheritanceMode, sources: List[PropertySource], case
    ) -> PropertyConflict:
        key = (name, mode, tuple(sources))
        conflict = self._conflicts.get(key)
        if conflict is None:
            conflict = PropertyConflict(name=name, mode=mode, sources=tuple(sources))
            self._conflicts[key] = conflict
        conflict.cases.append(case)
        return conflict

    def resolve_case(self, case: TestCase, chain: List[Dict]):
        names: List[str] = []
        for level in chain:
            for name in level:
                if name not in names:
                    names.append(name)
        dimensions = {}
        conflicts = []
        for name in names:
            mode = self.config.mode_for(name)
            if mode is InheritanceMode.NEAREST:
                level = next(lv for lv in reversed(chain) if name in lv)
                sources = level[name]
                values = {source.value for source in sources}
                if len(values) > 1:
                    conflicts.append(self._conflict(name, mode, sources, case))
                    continue
                dimensions[name] = ResolvedDimension(
                    name, mode, sources[0].value, tuple(sources)
                )
            elif mode is InheritanceMode.UNIFORM:
                sources = [source for level in chain for source in level.get(name, ())]
                values = {source.value for source in sources}
                if len(values) > 1:
                    conflicts.append(self._conflict(name, mode, sources, case))
                    continue
                dimensions[name] = ResolvedDimension(
                    name, mode, sources[0].value, tuple(sources)
                )
            else:  # ACCUMULATE
                sources = [source for level in chain for source in level.get(name, ())]
                dimensions[name] = ResolvedDimension(
                    name,
                    mode,
                    frozenset(source.value for source in sources),
                    tuple(sources),
                )
        self.cases.append(
            CaseDimensions(case=case, dimensions=dimensions, conflicts=conflicts)
        )


def resolve_properties(
    root: JUnitXml | TestSuite, config: InheritanceConfig | None = None
) -> InheritanceResult:
    """Resolve inherited testsuite properties for every testcase under *root*.

    *root* may be a :class:`JUnitXml` or a single :class:`TestSuite`.
    *config* defaults to an :class:`InheritanceConfig` where every property
    uses the ``NEAREST`` mode.
    """
    if config is None:
        config = InheritanceConfig()
    if isinstance(root, TestSuite):
        top_suites = [root]
    else:
        top_suites = list(root)
    resolver = _Resolver(config)
    for entry, suite in _sibling_entries(top_suites):
        resolver.walk(suite, (entry,), [])
    return InheritanceResult(resolver.cases, resolver.definitions)
