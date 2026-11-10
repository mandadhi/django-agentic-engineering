"""JSON-Schema validation for contracts and configuration files.

All schemas in the given directories are loaded into one ``referencing`` registry,
so cross-file ``$ref`` (``common.schema.json#/$defs/...``) resolves locally — no
network access is ever attempted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from .errors import ConfigError, ContractValidationError, ValidationIssue

SCHEMA_SUFFIX = ".schema.json"


def _no_remote(uri: str) -> Resource:
    raise NoSuchResource(ref=uri)


def _pointer(error: ValidationError) -> str:
    return "/".join(str(p) for p in error.absolute_path)


def _message(error: ValidationError) -> str:
    # For anyOf/oneOf failures the top-level message is vague; surface the best sub-error.
    if error.context:
        best = max(error.context, key=lambda e: len(e.absolute_path))
        return f"{error.message} (closest: {best.message})"
    return error.message


def schema_issues(schema: Mapping[str, Any], instance: Any, source: str) -> list[ValidationIssue]:
    """Violations of an inline (self-contained) JSON Schema, e.g. a workflow's ``inputs`` block."""
    validator = Draft202012Validator(schema, registry=Registry(retrieve=_no_remote), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(map(str, e.absolute_path)))
    return [ValidationIssue(source, _pointer(e), _message(e), "schema") for e in errors]


class SchemaSet:
    """A named collection of JSON Schemas (name = file name without ``.schema.json``)."""

    def __init__(self, directories: Iterable[Path]) -> None:
        self._schemas: dict[str, dict[str, Any]] = {}
        resources: list[tuple[str, Resource]] = []
        for directory in directories:
            directory = Path(directory)
            if not directory.is_dir():
                raise ConfigError(f"Schema directory not found: {directory}")
            for path in sorted(directory.glob(f"*{SCHEMA_SUFFIX}")):
                try:
                    schema = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ConfigError(f"Cannot read schema {path}: {exc}") from exc
                schema_id = schema.get("$id")
                if not schema_id:
                    raise ConfigError(f"Schema {path} has no $id")
                Draft202012Validator.check_schema(schema)
                name = path.name[: -len(SCHEMA_SUFFIX)]
                self._schemas[name] = schema
                resources.append((schema_id, Resource.from_contents(schema)))
        self._registry: Registry = Registry(retrieve=_no_remote).with_resources(resources)
        self._validators: dict[str, Draft202012Validator] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._schemas))

    def schema(self, name: str) -> Mapping[str, Any]:
        try:
            return self._schemas[name]
        except KeyError:
            raise ConfigError(f"Unknown schema '{name}'. Available: {', '.join(self.names)}") from None

    def _validator(self, name: str) -> Draft202012Validator:
        if name not in self._validators:
            self._validators[name] = Draft202012Validator(
                self.schema(name), registry=self._registry, format_checker=FormatChecker()
            )
        return self._validators[name]

    def issues(self, name: str, instance: Any, source: str | None = None) -> list[ValidationIssue]:
        """Every schema violation of ``instance``, ordered by location."""
        errors = sorted(self._validator(name).iter_errors(instance), key=lambda e: list(map(str, e.absolute_path)))
        return [ValidationIssue(source or name, _pointer(e), _message(e), "schema") for e in errors]


class ContractValidator(SchemaSet):
    """Validates AgentTask / AgentResult / WorkflowDefinition / WorkflowStage / EvaluationResult."""

    def __init__(self, contracts_dir: Path) -> None:
        super().__init__([contracts_dir])

    def validate(self, name: str, instance: Any, source: str | None = None) -> None:
        """Raise :class:`ContractValidationError` listing every violation."""
        found = self.issues(name, instance, source)
        if found:
            raise ContractValidationError(f"Invalid {name} instance", found)
