"""The draft-07 bundle of the AgentResult contract used for SDK structured output."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from agent_runtime.output_schema import DRAFT_07, bundle_schema, schema_fingerprint
from core import HARNESS_HOME

EXAMPLE = json.loads((HARNESS_HOME / "contracts" / "examples" / "agent-result.example.json").read_text("utf-8"))


@pytest.fixture
def bundled(registry):
    return bundle_schema(registry.contracts, "agent-result")


def test_bundle_is_self_contained_draft07(bundled) -> None:
    Draft7Validator.check_schema(bundled)
    assert bundled["$schema"] == DRAFT_07 and "$id" not in bundled and "$defs" not in json.dumps(bundled)
    refs = [part.split('"')[0] for part in json.dumps(bundled).split('"$ref": "')[1:]]
    assert refs and all(ref.startswith("#/definitions/") for ref in refs)
    for ref in refs:
        assert ref.split("/")[-1] in bundled["definitions"]


def test_bundle_is_deterministic(registry, bundled) -> None:
    again = bundle_schema(registry.contracts, "agent-result")
    assert json.dumps(again, sort_keys=False) == json.dumps(bundled, sort_keys=False)
    assert schema_fingerprint(again) == schema_fingerprint(bundled)


def _variants():
    yield "example", EXAMPLE
    for name, mutate in [
        ("failed-without-errors", lambda d: d.update(status="failed", errors=[])),
        ("extra-field", lambda d: d.update(colour="red")),
        ("bad-identifier", lambda d: d.update(agent_id="Not Valid")),
        ("bad-sha", lambda d: d["outputs"][0]["artifact"].update(sha256="xyz")),
        ("artifact-content", lambda d: d["outputs"][0]["artifact"].update(content="inline")),
        ("bad-status", lambda d: d.update(status="done")),
        ("negative-tokens", lambda d: d.update(metrics={"input_tokens": -1})),
        ("bad-error-code", lambda d: d.update(status="failed", errors=[{"code": "Bad Code", "message": "m"}])),
        ("wrong-kind", lambda d: d.update(kind="AgentTask")),
        ("blocked-with-error", lambda d: d.update(status="blocked", errors=[{"code": "x", "message": "m"}])),
    ]:
        doc = copy.deepcopy(EXAMPLE)
        mutate(doc)
        yield name, doc


@pytest.mark.parametrize("name, instance", list(_variants()), ids=[n for n, _ in _variants()])
def test_bundle_accepts_and_rejects_like_the_contract(registry, bundled, name, instance) -> None:
    original_ok = not registry.contracts.issues("agent-result", instance)
    bundled_ok = Draft7Validator(bundled).is_valid(instance)
    assert original_ok == bundled_ok
    assert original_ok == (name in ("example", "blocked-with-error"))


def test_sdk_projection_has_no_top_level_combinators(registry) -> None:
    """Real API (CLI 2.1.317): 'input_schema does not support oneOf, allOf, or anyOf at the top level'."""
    from agent_runtime.output_schema import UNSUPPORTED_TOP_LEVEL, structured_output_schema

    schema, removed = structured_output_schema(registry.contracts, "agent-result")
    Draft7Validator.check_schema(schema)
    assert removed == ("allOf",) and not set(UNSUPPORTED_TOP_LEVEL) & set(schema)
    assert schema["additionalProperties"] is False and schema["required"]            # everything else is kept


@pytest.mark.parametrize("name, instance", list(_variants()), ids=[n for n, _ in _variants()])
def test_sdk_projection_only_ever_widens(registry, name, instance) -> None:
    """The projection may accept more than the contract, never less; the processor enforces the rest."""
    from agent_runtime.output_schema import structured_output_schema

    schema, _ = structured_output_schema(registry.contracts, "agent-result")
    if not registry.contracts.issues("agent-result", instance):
        assert Draft7Validator(schema).is_valid(instance)
    if name == "failed-without-errors":
        assert Draft7Validator(schema).is_valid(instance)      # only the full contract catches this one
