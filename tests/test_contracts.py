import copy
import json

import pytest

from core import HARNESS_HOME
from core.contracts import ContractValidator
from core.errors import ContractValidationError

EXAMPLES = HARNESS_HOME / "contracts" / "examples"


@pytest.fixture(scope="module")
def contracts() -> ContractValidator:
    return ContractValidator(HARNESS_HOME / "contracts")


def example(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.example.json").read_text(encoding="utf-8"))


def test_all_contracts_present(contracts: ContractValidator) -> None:
    assert set(contracts.names) == {"common", "agent-task", "agent-result", "workflow-definition",
                                    "workflow-stage", "evaluation-result", "run-record"}


@pytest.mark.parametrize("name", ["agent-task", "agent-result", "evaluation-result"])
def test_examples_are_valid(contracts: ContractValidator, name: str) -> None:
    contracts.validate(name, example(name))


def test_task_without_objective(contracts: ContractValidator) -> None:
    task = example("agent-task")
    task["objective"] = ""
    with pytest.raises(ContractValidationError) as info:
        contracts.validate("agent-task", task)
    assert info.value.issues[0].pointer == "objective"


def test_task_requires_expected_outputs(contracts: ContractValidator) -> None:
    task = example("agent-task")
    task["expected_outputs"] = []
    with pytest.raises(ContractValidationError):
        contracts.validate("agent-task", task)


def test_task_rejects_unknown_fields_but_accepts_extensions(contracts: ContractValidator) -> None:
    task = example("agent-task")
    task["extensions"]["acme.anything"] = {"nested": True}
    contracts.validate("agent-task", task)
    task["priority"] = "high"
    with pytest.raises(ContractValidationError, match="'priority' was unexpected"):
        contracts.validate("agent-task", task)


def test_invalid_context_scope(contracts: ContractValidator) -> None:
    task = example("agent-task")
    task["context"]["refs"][0]["scope"] = "global"
    with pytest.raises(ContractValidationError) as info:
        contracts.validate("agent-task", task)
    assert info.value.issues[0].pointer == "context/refs/0/scope"


def test_failed_result_requires_errors(contracts: ContractValidator) -> None:
    result = example("agent-result")
    result["status"] = "failed"
    with pytest.raises(ContractValidationError):
        contracts.validate("agent-result", result)
    result["errors"] = [{"code": "tool.timeout", "message": "bandit timed out", "retryable": True}]
    contracts.validate("agent-result", result)


def test_artifact_ref_cannot_carry_content(contracts: ContractValidator) -> None:
    result = example("agent-result")
    result["outputs"][0]["artifact"]["content"] = "# full report inline"
    with pytest.raises(ContractValidationError, match="'content' was unexpected"):
        contracts.validate("agent-result", result)


def test_bad_checksum(contracts: ContractValidator) -> None:
    result = example("agent-result")
    result["outputs"][0]["artifact"]["sha256"] = "abc"
    with pytest.raises(ContractValidationError):
        contracts.validate("agent-result", result)


def test_unknown_status(contracts: ContractValidator) -> None:
    result = copy.deepcopy(example("agent-result"))
    result["status"] = "done"
    with pytest.raises(ContractValidationError) as info:
        contracts.validate("agent-result", result)
    assert info.value.issues[0].pointer == "status"


def test_evaluation_score_range(contracts: ContractValidator) -> None:
    evaluation = example("evaluation-result")
    evaluation["overall"]["score"] = 1.2
    with pytest.raises(ContractValidationError):
        contracts.validate("evaluation-result", evaluation)


def test_workflow_stage_contract(contracts: ContractValidator) -> None:
    stage = {"id": "audit", "agent_id": "security-auditor", "objective": "Audit",
             "expected_outputs": [{"name": "report", "type": "report"}]}
    contracts.validate("workflow-stage", stage)
    stage["max_attempts"] = 0
    with pytest.raises(ContractValidationError):
        contracts.validate("workflow-stage", stage)


def test_workflow_definition_version_must_be_semver(contracts: ContractValidator) -> None:
    definition = {"schema_version": "1.0", "kind": "WorkflowDefinition", "id": "x", "version": "1",
                  "stages": [{"id": "a", "agent_id": "b", "objective": "c",
                              "expected_outputs": [{"name": "d", "type": "text"}]}]}
    with pytest.raises(ContractValidationError) as info:
        contracts.validate("workflow-definition", definition)
    assert info.value.issues[0].pointer == "version"
