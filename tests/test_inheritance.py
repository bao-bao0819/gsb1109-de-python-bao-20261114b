import json

from src.junitparser import (
    InheritanceConfig,
    InheritanceMode,
    JUnitXml,
    PropertySource,
    resolve_properties,
)


def parse(text, config=None):
    xml = JUnitXml.fromstring(text)
    return resolve_properties(xml, config)


class TestNearestMode:
    def test_child_overrides_parent(self):
        text = """<testsuites>
          <testsuite name="batch">
            <properties><property name="region" value="default"/></properties>
            <testcase name="c0"/>
            <testsuite name="dc-hz1">
              <properties><property name="region" value="hz1"/></properties>
              <testcase name="c1"/>
            </testsuite>
          </testsuite>
        </testsuites>"""
        result = parse(text)
        assert not result.conflicts
        dims = {cd.case.name: cd for cd in result.resolved}
        assert dims["c0"]["region"].value == "default"
        assert dims["c1"]["region"].value == "hz1"

    def test_default_mode_applies_to_unconfigured_properties(self):
        text = """<testsuite name="top">
          <properties><property name="anything" value="a"/></properties>
          <testcase name="c0"/>
        </testsuite>"""
        result = parse(text, InheritanceConfig())
        assert result.resolved[0]["anything"].mode is InheritanceMode.NEAREST
        assert result.resolved[0]["anything"].value == "a"


class TestAccumulateMode:
    def test_values_accumulate_into_set(self):
        config = InheritanceConfig.from_dict({"labels": "accumulate"})
        text = """<testsuite name="top">
          <properties><property name="labels" value="smoke"/></properties>
          <testcase name="c0"/>
          <testsuite name="child">
            <properties><property name="labels" value="nightly"/></properties>
            <testcase name="c1"/>
          </testsuite>
        </testsuite>"""
        result = parse(text, config)
        dims = {cd.case.name: cd for cd in result.resolved}
        assert dims["c0"]["labels"].value == frozenset({"smoke"})
        assert dims["c1"]["labels"].value == frozenset({"smoke", "nightly"})
        sources = dims["c1"]["labels"].sources
        assert {s.path for s in sources} == {("top",), ("top", "child")}


class TestUniformMode:
    def test_consistent_path_resolves(self):
        config = InheritanceConfig.from_dict({"build": "uniform"})
        text = """<testsuite name="top">
          <properties><property name="build" value="100"/></properties>
          <testsuite name="child">
            <properties><property name="build" value="100"/></properties>
            <testcase name="c0"/>
          </testsuite>
        </testsuite>"""
        result = parse(text, config)
        assert not result.quarantined
        dim = result.resolved[0]["build"]
        assert dim.value == "100"
        assert len(dim.sources) == 2

    def test_inconsistent_path_quarantines_case(self):
        config = InheritanceConfig.from_dict({"build": "uniform"})
        text = """<testsuite name="top">
          <properties><property name="build" value="100"/></properties>
          <testsuite name="child">
            <properties><property name="build" value="200"/></properties>
            <testcase name="c0"/>
          </testsuite>
        </testsuite>"""
        result = parse(text, config)
        assert not result.resolved
        assert [cd.case.name for cd in result.quarantined] == ["c0"]
        conflict = result.conflicts[0]
        assert conflict.name == "build"
        assert {(s.path, s.value) for s in conflict.sources} == {
            (("top",), "100"),
            (("top", "child"), "200"),
        }
        assert [c.name for c in conflict.cases] == ["c0"]


class TestSameLevelConflict:
    def test_duplicate_unique_values_quarantine_case(self):
        text = """<testsuite name="top">
          <properties>
            <property name="region" value="x"/>
            <property name="region" value="y"/>
          </properties>
          <testcase name="c0"/>
        </testsuite>"""
        result = parse(text)
        assert not result.resolved
        assert [cd.case.name for cd in result.quarantined] == ["c0"]
        conflict = result.conflicts[0]
        assert conflict.name == "region"
        assert sorted(s.value for s in conflict.sources) == ["x", "y"]
        assert all(s.path == ("top",) for s in conflict.sources)

    def test_conflict_does_not_block_other_attributes(self):
        config = InheritanceConfig.from_dict({"labels": "accumulate"})
        text = """<testsuite name="top">
          <properties>
            <property name="region" value="x"/>
            <property name="region" value="y"/>
            <property name="labels" value="smoke"/>
          </properties>
          <testcase name="c0"/>
        </testsuite>"""
        result = parse(text, config)
        quarantined = result.quarantined[0]
        assert "region" not in quarantined.dimensions
        assert quarantined["labels"].value == frozenset({"smoke"})


class TestSameNamedSiblings:
    def test_siblings_resolve_independently(self):
        text = """<testsuite name="batch">
          <testsuite name="dc">
            <properties><property name="region" value="hz1"/></properties>
            <testcase name="c0"/>
          </testsuite>
          <testsuite name="dc">
            <properties><property name="region" value="hz2"/></properties>
            <testcase name="c1"/>
          </testsuite>
        </testsuite>"""
        result = parse(text)
        assert not result.conflicts
        dims = {cd.case.name: cd for cd in result.resolved}
        assert dims["c0"]["region"].value == "hz1"
        assert dims["c1"]["region"].value == "hz2"
        assert dims["c0"]["region"].sources[0].path == ("batch", "dc[1]")
        assert dims["c1"]["region"].sources[0].path == ("batch", "dc[2]")


class TestImpactQuery:
    def test_shadowed_definition_impacts_nothing(self):
        text = """<testsuite name="batch">
          <properties><property name="region" value="default"/></properties>
          <testcase name="c0"/>
          <testsuite name="dc-hz1">
            <properties><property name="region" value="hz1"/></properties>
            <testcase name="c1"/>
          </testsuite>
        </testsuite>"""
        result = parse(text)
        top = PropertySource(path=("batch",), name="region", value="default")
        child = PropertySource(path=("batch", "dc-hz1"), name="region", value="hz1")
        assert [c.name for c in result.impacted_cases(top)] == ["c0"]
        assert [c.name for c in result.impacted_cases(child)] == ["c1"]
        assert [c.name for c in result.impacted_by(("batch",), "region")] == ["c0"]
        assert [c.name for c in result.impacted_by("batch", "region")] == ["c0"]
        assert [c.name for c in result.impacted_by(("batch", "dc-hz1"), "region")] == [
            "c1"
        ]

    def test_accumulate_change_impacts_all_descendants(self):
        config = InheritanceConfig.from_dict({"labels": "accumulate"})
        text = """<testsuite name="top">
          <properties><property name="labels" value="smoke"/></properties>
          <testsuite name="child">
            <properties><property name="labels" value="nightly"/></properties>
            <testcase name="c0"/>
          </testsuite>
        </testsuite>"""
        result = parse(text, config)
        assert [c.name for c in result.impacted_by(("top",), "labels")] == ["c0"]
        assert [c.name for c in result.impacted_by(("top", "child"), "labels")] == [
            "c0"
        ]

    def test_unknown_definition_impacts_nothing(self):
        text = """<testsuite name="top">
          <properties><property name="region" value="x"/></properties>
          <testcase name="c0"/>
        </testsuite>"""
        result = parse(text)
        ghost = PropertySource(path=("nowhere",), name="region", value="y")
        assert result.impacted_cases(ghost) == []
        assert result.impacted_by(("nowhere",), "region") == []


class TestConfig:
    def test_from_dict(self):
        config = InheritanceConfig.from_dict(
            {
                "region": "nearest",
                "labels": "accumulate",
                "build": "uniform",
                "default": "accumulate",
            }
        )
        assert config.mode_for("region") is InheritanceMode.NEAREST
        assert config.mode_for("labels") is InheritanceMode.ACCUMULATE
        assert config.mode_for("build") is InheritanceMode.UNIFORM
        assert config.mode_for("other") is InheritanceMode.ACCUMULATE

    def test_from_json(self, tmp_path):
        path = tmp_path / "inheritance.json"
        path.write_text(json.dumps({"region": "nearest", "labels": "accumulate"}))
        config = InheritanceConfig.from_json(str(path))
        assert config.mode_for("labels") is InheritanceMode.ACCUMULATE
        assert config.mode_for("region") is InheritanceMode.NEAREST
        with open(path) as fp:
            config = InheritanceConfig.from_json(fp)
        assert config.mode_for("labels") is InheritanceMode.ACCUMULATE
