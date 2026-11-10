"""Bundle a multi-file contract into one self-contained draft-07 JSON Schema.

The AgentResult contract is split across files (``agent-result.schema.json`` refers
to ``common.schema.json#/$defs/...``). The SDK's structured output takes a single
schema (``--json-schema``) with no way to resolve other files, so this produces
one document:

* every reachable ``$ref`` target is copied into ``definitions`` and the ``$ref`` is
  rewritten to ``#/definitions/<Name>`` (local, offline, deterministic);
* ``$defs`` → ``definitions``, nested ``$id``/``$schema`` dropped, root ``$schema``
  set to draft-07;
* a ``$ref`` with sibling keywords is wrapped in ``allOf`` (draft-07 ignores
  siblings of ``$ref``), so no constraint is lost.

The contract itself is not changed or weakened; tests check the bundle accepts and
rejects the same instances as the original. What is actually sent to the SDK is
:func:`structured_output_schema`, which additionally drops top-level combinators the
API refuses; the full contract is still enforced on every result.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from core.contracts import SchemaSet

DRAFT_07 = "http://json-schema.org/draft-07/schema#"
SUFFIX = ".schema.json"


class SchemaBundleError(ValueError):
    pass


def _pointer_get(document: Any, pointer: str) -> Any:
    node = document
    for part in filter(None, pointer.lstrip("/").split("/")):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, ValueError) as exc:
            raise SchemaBundleError(f"unresolvable JSON pointer '{pointer}'") from exc
    return node


def bundle_schema(schemas: SchemaSet, name: str) -> dict[str, Any]:
    """Self-contained draft-07 version of contract ``name`` (e.g. ``"agent-result"``)."""
    definitions: dict[str, Any] = {}
    origin: dict[str, tuple[str, str]] = {}   # definition key -> (schema name, pointer)

    def key_for(schema_name: str, pointer: str) -> str:
        target = (schema_name, pointer)
        base = pointer.rsplit("/", 1)[-1] if pointer else schemas.schema(schema_name).get("title", schema_name)
        key = base
        if key in origin and origin[key] != target:
            key = f"{schema_name}.{base}"
        if key in origin and origin[key] != target:   # pragma: no cover - defensive
            raise SchemaBundleError(f"definition name collision for '{key}'")
        return key

    def resolve(ref: str, current: str) -> str:
        file_part, _, fragment = ref.partition("#")
        schema_name = current
        if file_part:
            file_name = file_part.rsplit("/", 1)[-1]
            if not file_name.endswith(SUFFIX):
                raise SchemaBundleError(f"unsupported $ref '{ref}' (only local contract files)")
            schema_name = file_name[: -len(SUFFIX)]
        pointer = fragment or ""
        if not file_part and not pointer:
            raise SchemaBundleError("recursive '#' references are not supported")
        key = key_for(schema_name, pointer)
        if key not in origin:
            origin[key] = (schema_name, pointer)
            definitions[key] = {}   # reserve before recursing (cycles)
            target = _pointer_get(schemas.schema(schema_name), pointer)
            definitions[key] = walk(target, schema_name)
        return f"#/definitions/{key}"

    def walk(node: Any, current: str) -> Any:
        if isinstance(node, list):
            return [walk(item, current) for item in node]
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$id", "$schema", "$defs", "$ref"):
                continue
            out[key] = walk(value, current)
        if "$ref" in node:
            ref = {"$ref": resolve(node["$ref"], current)}
            if not out:
                return ref
            out["allOf"] = [ref, *out.get("allOf", [])]
        return out

    root = schemas.schema(name)
    body = walk(root, name)
    bundled: dict[str, Any] = {"$schema": DRAFT_07}
    bundled.update(body)
    if definitions:
        bundled["definitions"] = {k: definitions[k] for k in sorted(definitions)}
    return bundled


#: Keywords the Messages API rejects at the top level of a tool ``input_schema`` — which is how
#: the CLI delivers ``--json-schema`` (observed with CLI 2.1.317: "input_schema does not support
#: oneOf, allOf, or anyOf at the top level"). Conditionals go with them.
UNSUPPORTED_TOP_LEVEL = ("allOf", "anyOf", "oneOf", "not", "if", "then", "else")


def structured_output_schema(schemas: SchemaSet, name: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The schema sent to the SDK: the draft-07 bundle without top-level combinators.

    This is a *projection* for transport, never the contract: it accepts a superset of what
    the contract accepts, and the result processor validates every result against the full
    contract afterwards (e.g. AgentResult's "failed/blocked ⇒ at least one error" rule, which
    lives in a top-level ``allOf``). Returns ``(schema, removed_keywords)``.
    """
    schema = bundle_schema(schemas, name)
    removed = tuple(k for k in UNSUPPORTED_TOP_LEVEL if k in schema)
    for key in removed:
        del schema[key]
    return schema, removed


def schema_fingerprint(schema: Mapping[str, Any]) -> str:
    """Stable hash of a bundled schema (recorded with each run for traceability)."""
    import hashlib

    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
