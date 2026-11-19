"""The output schema check, the timing statistics, derived random streams and METRIC/ASI lines."""

import json
import re
import statistics
from pathlib import Path

import pytest

from commonhill_grader import (
    PUBLIC_SEED,
    Checker,
    Timings,
    Value,
    cv,
    is_local,
    metric_lines,
    percentile,
    rng,
    seed,
    summary,
    write_result,
)

# --- schema ----------------------------------------------------------------------------------------

LABEL = {
    "type": "object",
    "properties": {
        "label": {"enum": ["cat", "dog"]},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "tags": {"type": "array", "items": {"type": "string", "maxLength": 3}, "maxItems": 2},
    },
    "required": ["label", "score"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ({"label": "cat", "score": 0.5}, None),
        ({"label": "dog", "score": 1, "tags": ["a", "bc"]}, None),
        ({"label": "cow", "score": 0.5}, "$.label: not one of the allowed values"),
        ({"label": "cat"}, "$.score: missing"),
        ({"label": "cat", "score": True}, "$.score: expected number"),
        ({"label": "cat", "score": 1.5}, "$.score: outside maximum 1"),
        ({"label": "cat", "score": float("nan")}, "$.score: not a finite number"),
        ({"label": "cat", "score": 0, "tags": ["abcd"]}, "$.tags[0]: longer than maxLength"),
        ({"label": "cat", "score": 0, "tags": ["a", "b", "c"]}, "$.tags: more than maxItems"),
        ({"label": "cat", "score": 0, "extra": 1}, "$: unexpected property"),
        (["cat", 0.5], "$: expected object"),
    ],
)
def test_the_checker_names_the_path_not_the_value(value: object, problem: str | None) -> None:
    assert Checker(LABEL)(value) == problem


@pytest.mark.parametrize(
    ("schema", "value", "fits"),
    [
        ({"type": "integer"}, 3, True),
        ({"type": "integer"}, 3.0, True),  # JSON has no separate integer type
        ({"type": "integer"}, 3.5, False),
        ({"type": "integer"}, True, False),
        ({"type": "number"}, False, False),
        ({"type": ["string", "null"]}, None, True),
        ({"const": 1}, 1.0, True),
        ({"const": 1}, True, False),
        ({"const": [1, {"a": True}]}, [1, {"a": True}], True),
        ({"const": [1, {"a": True}]}, [1, {"a": 1}], False),
        ({"exclusiveMinimum": 0}, 0, False),
        ({"minLength": 2}, "a", False),
        ({"additionalProperties": {"type": "integer"}}, {"a": 1, "b": "x"}, False),
    ],
)
def test_the_checker_follows_json_semantics(schema: dict[str, object], value: object, fits: bool) -> None:
    assert (Checker(schema)(value) is None) is fits


BIG = 2**53  # beyond it, not every integer is a float


@pytest.mark.parametrize(
    ("schema", "value", "fits"),
    [
        # Huge integers: exact, never converted to float, never an OverflowError.
        ({"type": "integer"}, 10**309, True),
        ({"type": "integer"}, 10**400, True),
        ({"type": "integer", "maximum": 10}, 10**400, False),
        ({"type": "integer", "minimum": 0}, 10**400, True),
        ({"type": "number", "minimum": -(10**400)}, -(10**400), True),
        ({"type": "number", "exclusiveMinimum": -(10**400)}, -(10**400), False),
        ({"type": "number", "maximum": 1e308}, 10**309, False),
        # Mixed int/float bounds compare exactly, inclusive and exclusive, on both sides.
        ({"minimum": BIG + 1}, float(BIG), False),
        ({"minimum": float(BIG)}, BIG + 1, True),
        ({"minimum": BIG + 1}, BIG + 1, True),
        ({"exclusiveMinimum": float(BIG)}, BIG + 1, True),
        ({"exclusiveMinimum": BIG + 1}, BIG + 1, False),
        ({"exclusiveMinimum": float(BIG)}, BIG, False),
        ({"maximum": float(BIG)}, BIG + 1, False),
        ({"maximum": BIG + 1}, float(BIG), True),
        ({"maximum": BIG}, float(BIG), True),
        ({"exclusiveMaximum": BIG + 1}, float(BIG), True),
        ({"exclusiveMaximum": float(BIG)}, BIG, False),
        ({"exclusiveMaximum": BIG + 1}, BIG + 1, False),
        # enum/const: numbers by value, booleans apart, objects regardless of key order.
        ({"enum": [1, "a"]}, 1.0, True),
        ({"enum": [1, "a"]}, True, False),
        ({"enum": [True]}, 1, False),
        ({"enum": [10**400]}, 10**400, True),
        ({"enum": [10**400]}, 10**400 + 1, False),
        ({"const": {"a": 1, "b": [0.5]}}, {"b": [0.5], "a": 1.0}, True),
        ({"const": {"a": 1, "b": [0.5]}}, {"b": [0.5], "a": 2}, False),
        ({"const": None}, None, True),
        ({"enum": [0.1]}, 0.1, True),
        ({"enum": [0.1]}, 0.1000000001, False),
        # Non-finite numbers never fit (the Client's decoder refuses them earlier).
        ({}, float("nan"), False),
        ({"type": "array"}, [float("inf")], True),  # unconstrained items aren't looked at
        ({"type": "array", "items": {}}, [float("inf")], False),
    ],
)
def test_numbers_are_exact(schema: dict[str, object], value: object, fits: bool) -> None:
    assert (Checker(schema)(value) is None) is fits


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"type": "string", "pattern": "^a"}, r"unsupported keywords \['pattern'\]"),
        ({"items": {"maxlength": 3}}, r"schema at \$\[\]: unsupported keywords \['maxlength'\]"),
        ({"type": "str"}, r"schema at \$: type must be one of"),
        ({"type": []}, r"schema at \$: type must be"),
        ({"minimum": "0"}, r"schema at \$: minimum must be a finite number"),
        ({"maximum": True}, r"schema at \$: maximum must be a finite number"),
        ({"exclusiveMinimum": float("inf")}, r"schema at \$: exclusiveMinimum must be a finite number"),
        ({"enum": "cat"}, r"schema at \$: enum must be a non-empty array"),
        ({"enum": []}, r"schema at \$: enum must be a non-empty array"),
        ({"enum": [float("nan")]}, r"schema at \$: enum and const must hold JSON values"),
        ({"const": {1: "a"}}, r"schema at \$: enum and const must hold JSON values"),
        ({"properties": []}, r"schema at \$: properties must be an object of schemas"),
        ({"properties": {"a": {"minLength": -1}}}, r"schema at \$\.a: minLength must be an integer >= 0"),
        ({"items": [{"type": "string"}]}, r"schema at \$\[\] must be an object"),
        ({"maxItems": 2.0}, r"schema at \$: maxItems must be an integer >= 0"),
        ({"required": "label"}, r"schema at \$: required must be an array of strings"),
        ({"additionalProperties": 1}, r"schema at \$\.\* must be an object"),
        ({"title": 3}, r"schema at \$: title must be a string"),
    ],
)
def test_a_schema_outside_the_subset_is_refused(schema: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Checker(schema)


# --- timing ----------------------------------------------------------------------------------------


def test_summary_statistics() -> None:
    values = [10.0, 12.0, 11.0, 13.0, 9.0, 30.0]
    stats = summary(values)
    assert stats["median"] == statistics.median(values) == 11.5
    assert stats["p90"] == pytest.approx(21.5)  # between 13 and 30
    assert stats["cv"] == pytest.approx(statistics.stdev(values) / statistics.fmean(values))
    assert (stats["n"], stats["min"], stats["max"], stats["total"]) == (6, 9.0, 30.0, 85.0)
    assert percentile([5.0], 90) == 5.0
    assert percentile([1.0, 2.0], 0) == 1.0
    assert percentile([1.0, 2.0], 100) == 2.0
    assert cv([4.0]) == 0.0
    with pytest.raises(ValueError, match="no values"):
        percentile([], 50)


def test_timings_per_batch_and_per_item() -> None:
    timings = Timings()
    timings.record(0.2, 100)
    timings.record(0.4, 100)
    timings.record(0.05, 10)
    assert timings.item_s == pytest.approx([0.002, 0.004, 0.005])
    result = timings.as_result()
    assert result["total_ms"] == pytest.approx(650)
    assert result["batches"] == 3
    assert result["batch_median_ms"] == pytest.approx(200)
    assert result["item_median_ms"] == pytest.approx(4)
    assert result["batch_cv"] == pytest.approx(
        statistics.stdev([0.2, 0.4, 0.05]) / statistics.fmean([0.2, 0.4, 0.05])
    )
    assert Timings().as_result() == {}
    with pytest.raises(ValueError, match="at least one item"):
        timings.record(0.1, 0)


# --- seeds -----------------------------------------------------------------------------------------

SECRET = "s3cr3t-platform-seed"


def test_rng_streams_are_reproducible_independent_and_keyed_by_the_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AR_SEED", raising=False)
    public = rng("order").random()
    assert rng("order").random() == public
    assert rng("inputs").random() != public
    monkeypatch.setenv("AR_SEED", SECRET)
    assert rng("order").random() != public


def test_the_secret_appears_nowhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AR_SEED", SECRET)
    assert seed() == SECRET
    stream = rng("order")
    assert SECRET not in repr(stream) + str(stream.getstate())
    write_result(primary=Value("x", 1.0), n_items=1, failures=0, grader_version="g", path=tmp_path / "r.json")
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")  # on the platform nothing is printed at all
    assert SECRET not in (tmp_path / "r.json").read_text()


# --- METRIC / ASI ----------------------------------------------------------------------------------

# omp autoresearch's patterns (packages/coding-agent/src/autoresearch/helpers.ts).
METRIC = re.compile(r"^METRIC\s+([\w.µ-]+)=(\S+)\s*$")
ASI = re.compile(r"^ASI\s+([\w.-]+)=(.+)\s*$")


def test_a_local_run_prints_metric_and_asi_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AR_SEED", raising=False)
    write_result(
        primary=Value("wall ms", 812.5),
        n_items=10,
        failures=0,
        grader_version="example 2",
        secondary={"p90": 9.0},
        public=Value("wall ms", 800.0),
        timings={"total_ms": 812.5},
        path=tmp_path / "r.json",
    )
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "METRIC wall_ms=812.5",
        "METRIC p90=9.0",
        "METRIC public.wall_ms=800.0",
        "ASI valid=true",
        "ASI n_items=10",
        "ASI failures=0",
        "ASI grader_version=example 2",
        "ASI timings.total_ms=812.5",
    ]
    assert all(METRIC.match(line) or ASI.match(line) for line in lines)


def test_an_invalid_run_prints_no_primary_metric() -> None:
    result = {
        "primary": {"name": "wall_ms", "value": None},
        "secondary": {},
        "public": None,
        "n_items": 3,
        "failures": 3,
        "timings": {},
        "grader_version": "g",
    }
    assert metric_lines(json.loads(json.dumps(result))) == [
        "ASI valid=false",
        "ASI n_items=3",
        "ASI failures=3",
        "ASI grader_version=g",
    ]


def test_an_empty_seed_is_a_local_run_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`AR_SEED=""` means unset: the public seed, the public streams, and METRIC/ASI printed."""
    monkeypatch.delenv("AR_SEED", raising=False)
    unset = rng("order").random()
    monkeypatch.setenv("AR_SEED", "")
    assert (seed(), is_local(), rng("order").random()) == (PUBLIC_SEED, True, unset)
    write_result(primary=Value("x", 1.0), n_items=1, failures=0, grader_version="g", path=tmp_path / "r.json")
    assert capsys.readouterr().out.splitlines()[0] == "METRIC x=1.0"
    monkeypatch.setenv("AR_SEED", SECRET)
    assert is_local() is False


def nested(depth: int, leaf: object = 0) -> object:
    value: object = leaf
    for _ in range(depth):
        value = [value]
    return value


def const_at_every_level(depth: int) -> dict[str, object]:
    schema: dict[str, object] = {"const": 0}
    for level in range(1, depth + 1):
        schema = {"type": "array", "items": schema, "const": nested(level)}
    return schema


@pytest.mark.parametrize("output", [nested(240), nested(240, 1), nested(239), [nested(239), 0]])
def test_nested_enum_and_const_encode_each_output_once(
    output: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A const at every level of a 240-deep array: the output is encoded once, by the outermost
    const (overlapping re-encoding would be 58 081 characters for 481)."""
    import commonhill_grader.schema as schema_module

    checker = Checker(const_at_every_level(240))
    encoded: list[int] = []
    real = schema_module.canonical

    def counting(value: object) -> str:
        text = real(value)
        encoded.append(len(text))
        return text

    monkeypatch.setattr(schema_module, "canonical", counting)
    fits = checker(output) is None
    assert fits is (output == nested(240))
    assert sum(encoded) <= len(json.dumps(output, separators=(",", ":")))


def test_enum_options_that_fail_their_own_schema_never_match() -> None:
    checker = Checker({"type": "integer", "minimum": 0, "enum": [-1, 2, "x", 3.5]})
    assert [checker(v) is None for v in (-1, 2, 2.0, "x", 3.5)] == [False, True, True, False, False]
    nested_enum = Checker({"items": {"enum": [1, 2]}, "enum": [[1], [3], [1, 2]]})
    assert [nested_enum(v) is None for v in ([1], [3], [1, 2], [2])] == [True, False, True, False]
    assert Checker({"const": 5, "maximum": 4})(5) == "$: not one of the allowed values"
