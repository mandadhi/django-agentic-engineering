"""Configuration loading: YAML → version check → JSON-Schema → typed, frozen objects.

``framework-config.yaml`` is the only file whose location is fixed (and even that
can be overridden). It names every other configuration file, so no agent, skill
or workflow name appears in application code.

Errors are collected across all files and raised once:
* :class:`ConfigVersionError` if any file declares an unsupported ``schema_version``;
* :class:`ConfigError` for unreadable files, wrong ``kind``, schema violations,
  duplicate ids and invalid regular expressions.

Cross-file references (agent → skill, stage → agent, ...) are checked later by
:mod:`core.validation`, once workflow definitions have been discovered.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import yaml

from . import HARNESS_HOME
from .contracts import SchemaSet
from .errors import ConfigError, ConfigVersionError, ValidationIssue
from .log import get_logger

if TYPE_CHECKING:
    from .orchestration import OrchestrationPolicy

log = get_logger(__name__)

SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})
DEFAULT_FRAMEWORK_FILE = "config/framework-config.yaml"
ALL_SCOPES: tuple[str, ...] = ("project", "workflow", "task", "agent")

#: Harness profiles. ``full`` (default) requires every registered plugin, as before. ``pack`` explicitly
#: excludes plugins registered as ``optional`` (parent-repository integrations) together with their agents,
#: skills, the workflows that use those agents and the policy entries that name them, and reports each
#: exclusion. Nothing optional disappears silently, and no security rule for a remaining agent is dropped.
PROFILES: tuple[str, ...] = ("full", "pack")
DEFAULT_PROFILE = "full"
PROFILE_ENV = "DAE_HARNESS_PROFILE"
#: Optional config file: framework-config ``configs.orchestration`` (canonical orchestration contract).
ORCHESTRATION_KEY = "orchestration"

# key under framework-config `configs:` → (expected `kind`, schema name)
_CONFIG_FILES: dict[str, tuple[str, str]] = {
    "agent_registry": ("AgentRegistry", "agent-registry"),
    "skill_registry": ("SkillRegistry", "skill-registry"),
    "workflow": ("WorkflowConfig", "workflow-config"),
    "evaluation": ("EvaluationConfig", "evaluation-config"),
    "security": ("SecurityConfig", "security-config"),
}


# --------------------------------------------------------------------------- models


@dataclass(frozen=True)
class ContextNeeds:
    scopes: tuple[str, ...] = ALL_SCOPES
    tags: tuple[str, ...] = ()
    token_budget: int | None = None


@dataclass(frozen=True)
class PluginSpec:
    """A Claude Code plugin directory the Agent SDK may load (``plugins`` option)."""

    id: str             # = .claude-plugin/plugin.json `name`
    path: Path          # absolute plugin root
    rel_path: str
    base: str = "repo_root"                     # what rel_path is relative to: repo_root | harness
    anchor: Path | None = None                  # absolute directory for `base`; the plugin must stay inside it
    read_paths: tuple[str, ...] = ()            # readable sub-folders at run time; () = the whole plugin
    agent_policy: str | None = None             # PluginSecurityPolicy file, relative to the plugin root
    load_hooks: bool = True                     # False: the plugin's own hooks are not loaded in harness sessions
    preload_skills: bool = False                # True: the harness injects registered SKILL.md files itself
    optional: bool = False                      # True: an integration the ``pack`` profile may exclude
    description: str = ""                       # what the plugin provides (shown when missing or excluded)

    @property
    def manifest(self) -> Path:
        return self.path / ".claude-plugin" / "plugin.json"

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Directories an agent may read at run time (resolved, so ``..`` cannot escape)."""
        if not self.read_paths:
            return (self.path,)
        return tuple((self.path / rel).resolve() for rel in self.read_paths)

    @property
    def agent_policy_path(self) -> Path | None:
        return (self.path / self.agent_policy).resolve() if self.agent_policy else None


@dataclass(frozen=True)
class AgentSpec:
    """Registry entry (harness data) + what the agent's Markdown file declares.

    The registry supplies id, source, path, role, skills, context_needs and enabled.
    description, model, tools, disallowed_tools and sdlc_phases are filled from the
    agent file's frontmatter by :class:`core.registry.HarnessRegistry` — never duplicated.
    """

    id: str
    source: str
    path: Path          # absolute
    rel_path: str       # as written in the registry (relative to repo_root)
    role: str | None = None
    skills: tuple[str, ...] = ()
    context_needs: ContextNeeds = field(default_factory=ContextNeeds)
    enabled: bool = True
    plugin: str | None = None                   # registered plugin containing the file
    # --- from frontmatter
    description: str = ""
    model: str | None = None
    tools: tuple[str, ...] | None = None        # None = not declared (inherits all tools)
    disallowed_tools: tuple[str, ...] = ()
    sdlc_phases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillSpec:
    id: str
    source: str
    path: Path          # absolute skill directory
    rel_path: str
    tags: tuple[str, ...] = ()
    enabled: bool = True
    plugin: str | None = None                   # registered plugin containing the skill
    description: str = ""                      # from SKILL.md frontmatter

    @property
    def sdk_name(self) -> str:
        """Name the Agent SDK knows the skill by: plugin skills are namespaced ``<plugin>:<skill>``."""
        return f"{self.plugin}:{self.id}" if self.plugin else self.id


@dataclass(frozen=True)
class WorkflowDefaults:
    max_attempts: int = 2
    stage_timeout_seconds: int = 1800
    approval_required: bool = False
    checkpoint_policy: str = "after_each_stage"


@dataclass(frozen=True)
class WorkflowSettings:
    definitions_dir: Path
    pattern: str
    defaults: WorkflowDefaults


@dataclass(frozen=True)
class EvaluationCriterion:
    id: str
    description: str
    scorer: str
    threshold: float
    weight: float = 1.0


@dataclass(frozen=True)
class EvaluationTarget:
    type: str          # agent | stage | workflow
    id: str            # stage targets use "<workflow>/<stage>"
    criteria: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationConfig:
    pass_policy: str
    overall_threshold: float | None
    criteria: tuple[EvaluationCriterion, ...]
    targets: tuple[EvaluationTarget, ...]


@dataclass(frozen=True)
class ToolOverride:
    agent_id: str
    deny: tuple[str, ...]
    write_mode: str | None = None     # harness-enforced write mode (SEC-05)


@dataclass(frozen=True)
class ModeCapabilities:
    """What one write mode may do (security-config ``write_modes``, SEC-06). Default deny."""

    allow: frozenset[str]
    otherwise: str = "deny"           # deny | approve: what happens to a capability not in ``allow``
    shell: bool = True                # False: Bash is refused in this mode


#: Built-in write-mode capabilities, used for any mode the configuration does not state.
#: The shipped security-config.yaml states the same values explicitly.
DEFAULT_WRITE_MODES: Mapping[str, ModeCapabilities] = {
    "read-only": ModeCapabilities(frozenset({"read"}), "deny"),
    "documentation-only": ModeCapabilities(frozenset({"read", "write"}), "deny", shell=False),
    "confirm-writes": ModeCapabilities(frozenset({"read"}), "approve"),
    "workspace-write": ModeCapabilities(frozenset({"read", "write", "delete", "execute", "network", "unknown"})),
}


@dataclass(frozen=True)
class SecretPattern:
    id: str
    regex: str


@dataclass(frozen=True)
class CommandRule:
    id: str
    regex: str
    description: str = ""


#: Defaults of security-config ``shell`` (Phase 5.1). The shipped file states them explicitly.
DEFAULT_DESTRUCTIVE_PROGRAMS = ("rm", "rmdir", "unlink", "shred", "truncate", "srm", "wipe")
DEFAULT_SENSITIVE_VARIABLES = r"(?i)(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE|DSN|DATABASE_URL)"


@dataclass(frozen=True)
class ShellPolicy:
    """How Bash may run (security-config ``shell``). Command analysis is policy; the OS sandbox is the boundary."""

    without_sandbox: str = "disable"            # disable | approve: where no OS sandbox is available
    allowed_domains: tuple[str, ...] = ()       # network access of sandboxed commands
    destructive_programs: tuple[str, ...] = DEFAULT_DESTRUCTIVE_PROGRAMS
    sensitive_variables: str = DEFAULT_SENSITIVE_VARIABLES


@dataclass(frozen=True)
class EnvironmentPolicy:
    """What reaches the Claude Code CLI process and every command it runs (security-config ``environment``).

    ``None`` = no environment section: the harness still never forwards unlisted values, but with the
    built-in defaults of :mod:`agent_runtime.environment`.
    """

    allow: tuple[str, ...] = ()
    credentials: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityConfig:
    denied_tools: tuple[str, ...]
    tool_overrides: tuple[ToolOverride, ...]
    denied_paths: tuple[str, ...]
    secret_patterns: tuple[SecretPattern, ...]
    redaction_enabled: bool
    redaction_replacement: str
    max_inline_bytes: int
    denied_commands: tuple[CommandRule, ...] = ()
    approval_commands: tuple[CommandRule, ...] = ()
    shell: ShellPolicy = field(default_factory=ShellPolicy)
    environment: EnvironmentPolicy | None = None
    write_modes: Mapping[str, ModeCapabilities] = field(default_factory=lambda: dict(DEFAULT_WRITE_MODES))

    def write_mode_of(self, agent_id: str) -> str | None:
        """The harness write mode security-config assigns to ``agent_id`` (``tools.agents[].write_mode``)."""
        return next((o.write_mode for o in self.tool_overrides if o.agent_id == agent_id and o.write_mode), None)


@dataclass(frozen=True)
class StorageSettings:
    backend: str
    sqlite_path: Path | None
    max_json_bytes: int = 65536


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    format: str = "json"


@dataclass(frozen=True)
class ContextSettings:
    default_token_budget: int = 32000
    reserve_tokens: int = 4000
    chars_per_token: float = 4.0


@dataclass(frozen=True)
class AgentRuntimeSettings:
    """Limits for one Agent SDK query (one stage attempt). See framework-config ``agent_runtime``."""

    max_turns: int | None = None
    max_budget_usd: float | None = None
    max_agent_seconds: int | None = None
    hook_timeout_seconds: int = 600
    structured_output: bool = True


@dataclass(frozen=True)
class FrameworkConfig:
    name: str
    version: str
    schema_version: str
    root: Path
    repo_root: Path
    contracts_dir: Path
    artifacts_dir: Path
    storage: StorageSettings
    logging: LoggingSettings
    context: ContextSettings
    agent_runtime: AgentRuntimeSettings = field(default_factory=AgentRuntimeSettings)


@dataclass(frozen=True)
class HarnessConfig:
    framework: FrameworkConfig
    agents: tuple[AgentSpec, ...]
    skills: tuple[SkillSpec, ...]
    workflow: WorkflowSettings
    evaluation: EvaluationConfig
    security: SecurityConfig
    sources: Mapping[str, Path]   # config key ("framework", "agent_registry", ...) → file
    plugins: tuple[PluginSpec, ...] = ()
    orchestration: "OrchestrationPolicy | None" = None
    profile: str = DEFAULT_PROFILE
    #: What the profile excluded: "plugin:<id>", "agent:<id>", "skill:<id>" (``pack`` profile only).
    excluded: tuple[str, ...] = ()
    excluded_plugins: tuple[PluginSpec, ...] = ()

    def label(self, key: str) -> str:
        """Human-friendly path of a config file, for error messages."""
        return relative_label(self.sources[key], self.framework.root)


# --------------------------------------------------------------------------- helpers


@lru_cache(maxsize=1)
def config_schemas() -> SchemaSet:
    """Framework-owned schemas for the config files (plus the shared contract defs)."""
    return SchemaSet([HARNESS_HOME / "contracts", HARNESS_HOME / "config" / "schemas"])


def relative_label(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve(base: Path, value: str) -> Path:
    return (base / value).resolve()


def load_yaml(path: Path, label: str) -> Any:
    """Parse a YAML file, turning I/O and syntax problems into ConfigError."""
    if not path.is_file():
        raise ConfigError("Configuration file not found",
                          [ValidationIssue(label, "", f"file does not exist: {path}", "missing-file")])
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError("Invalid YAML", [ValidationIssue(label, where, problem, "yaml")]) from exc
    except OSError as exc:
        raise ConfigError("Cannot read configuration file",
                          [ValidationIssue(label, "", str(exc), "io")]) from exc


def check_document(doc: Any, label: str, kind: str, schema_name: str) -> list[ValidationIssue]:
    """Version, kind and schema checks for one configuration document."""
    if not isinstance(doc, dict):
        return [ValidationIssue(label, "", f"expected a mapping, got {type(doc).__name__}", "kind")]
    version = doc.get("schema_version")
    if version is not None and str(version) not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        return [ValidationIssue(label, "schema_version",
                                f"schema_version '{version}' is not supported (supported: {supported})",
                                "version")]
    issues: list[ValidationIssue] = []
    if doc.get("kind") != kind:
        issues.append(ValidationIssue(label, "kind", f"expected kind '{kind}', found '{doc.get('kind')}'", "kind"))
    issues.extend(config_schemas().issues(schema_name, doc, label))
    return issues


def _duplicates(items: list[Mapping[str, Any]], label: str, collection: str) -> list[ValidationIssue]:
    counts = Counter(item.get("id") for item in items if isinstance(item, Mapping))
    return [
        ValidationIssue(label, f"{collection}/{index}/id", f"duplicate id '{item['id']}'", "duplicate-id")
        for index, item in enumerate(items)
        if isinstance(item, Mapping) and counts[item.get("id")] > 1
    ]


def _semantic_issues(key: str, doc: Mapping[str, Any], label: str) -> list[ValidationIssue]:
    """Checks JSON Schema cannot express: unique ids, compilable regexes."""
    issues: list[ValidationIssue] = []
    if key == "agent_registry":
        issues += _duplicates(doc.get("agents", []), label, "agents")
        issues += _duplicates(doc.get("plugins", []), label, "plugins")
    elif key == "skill_registry":
        issues += _duplicates(doc.get("skills", []), label, "skills")
    elif key == "evaluation":
        issues += _duplicates(doc.get("criteria", []), label, "criteria")
    elif key == "security":
        groups = {"secrets/patterns": doc.get("secrets", {}).get("patterns", []),
                  "commands/deny": doc.get("commands", {}).get("deny", []),
                  "commands/require_approval": doc.get("commands", {}).get("require_approval", [])}
        for where, rules in groups.items():
            issues += _duplicates(rules, label, where)
            for index, rule in enumerate(rules):
                try:
                    re.compile(rule["regex"])
                except re.error as exc:
                    issues.append(ValidationIssue(label, f"{where}/{index}/regex",
                                                  f"invalid regular expression: {exc}", "invalid-regex"))
        sensitive = doc.get("shell", {}).get("sensitive_variables")
        if sensitive is not None:
            try:
                re.compile(sensitive)
            except re.error as exc:
                issues.append(ValidationIssue(label, "shell/sensitive_variables",
                                              f"invalid regular expression: {exc}", "invalid-regex"))
    return issues


def _raise(issues: list[ValidationIssue], message: str) -> None:
    if any(issue.code == "version" for issue in issues):
        raise ConfigVersionError(message, issues)
    raise ConfigError(message, issues)


# --------------------------------------------------------------------------- builders


def _framework(doc: Mapping[str, Any], root: Path) -> FrameworkConfig:
    paths, storage, ctx, logcfg = doc["paths"], doc["storage"], doc.get("context", {}), doc.get("logging", {})
    runtime = doc.get("agent_runtime", {})
    sqlite = storage.get("sqlite")
    return FrameworkConfig(
        name=doc["framework"]["name"],
        version=doc["framework"]["version"],
        schema_version=str(doc["schema_version"]),
        root=root,
        repo_root=_resolve(root, paths["repo_root"]),
        contracts_dir=_resolve(root, paths["contracts_dir"]),
        artifacts_dir=_resolve(root, paths["artifacts_dir"]),
        storage=StorageSettings(
            backend=storage["backend"],
            sqlite_path=_resolve(root, sqlite["path"]) if sqlite else None,
            max_json_bytes=storage.get("max_json_bytes", StorageSettings.max_json_bytes),
        ),
        logging=LoggingSettings(**logcfg),
        context=ContextSettings(**ctx),
        agent_runtime=AgentRuntimeSettings(**runtime),
    )


#: ``base: harness`` anchors a plugin at the harness installation itself (the folder holding
#: ``core/``), not at the copy of config/ in use, so it never depends on the working directory.
_PLUGIN_BASES = {"harness": HARNESS_HOME}


def _plugin(item: Mapping[str, Any], repo_root: Path) -> PluginSpec:
    base = item.get("base", "repo_root")
    anchor = _PLUGIN_BASES.get(base, repo_root).resolve()
    return PluginSpec(id=item["id"], path=_resolve(anchor, item["path"]), rel_path=item["path"], base=base,
                      anchor=anchor, read_paths=tuple(item.get("read_paths", ())),
                      agent_policy=item.get("agent_policy"), load_hooks=item.get("load_hooks", True),
                      preload_skills=item.get("preload_skills", False), optional=item.get("optional", False),
                      description=item.get("description", ""))


def plugin_containing(path: Path, plugins: tuple[PluginSpec, ...]) -> str | None:
    """Id of the registered plugin whose directory contains ``path`` (resolved, so ``..`` cannot escape)."""
    for plugin in plugins:
        if path == plugin.path or plugin.path in path.parents:
            return plugin.id
    return None


def _located(item: Mapping[str, Any], repo_root: Path, plugins: tuple[PluginSpec, ...]) -> tuple[Path, str | None]:
    """Absolute path and plugin id of a registry entry.

    With ``plugin: <id>`` the path is relative to that plugin's root and the entry belongs to it
    (an unregistered id is kept so validation can name it). Otherwise the path is relative to
    repo_root and a ``source: plugin`` entry belongs to the registered plugin that contains it.
    """
    declared = item.get("plugin")
    if declared:
        owner = next((p for p in plugins if p.id == declared), None)
        return _resolve(owner.path if owner else repo_root, item["path"]), declared
    path = _resolve(repo_root, item["path"])
    return path, plugin_containing(path, plugins) if item["source"] == "plugin" else None


def _agent(item: Mapping[str, Any], repo_root: Path, plugins: tuple[PluginSpec, ...]) -> AgentSpec:
    needs = item.get("context_needs", {})
    path, plugin = _located(item, repo_root, plugins)
    return AgentSpec(
        id=item["id"],
        source=item["source"],
        path=path,
        rel_path=item["path"],
        role=item.get("role"),
        skills=tuple(item.get("skills", ())),
        context_needs=ContextNeeds(
            scopes=tuple(needs.get("scopes", ALL_SCOPES)),
            tags=tuple(needs.get("tags", ())),
            token_budget=needs.get("token_budget"),
        ),
        enabled=item.get("enabled", True),
        plugin=plugin,
    )


def _skill(item: Mapping[str, Any], repo_root: Path, plugins: tuple[PluginSpec, ...]) -> SkillSpec:
    path, plugin = _located(item, repo_root, plugins)
    return SkillSpec(
        id=item["id"],
        source=item["source"],
        path=path,
        rel_path=item["path"],
        tags=tuple(item.get("tags", ())),
        enabled=item.get("enabled", True),
        plugin=plugin,
    )


def _workflow(doc: Mapping[str, Any], root: Path) -> WorkflowSettings:
    discovery = doc["discovery"]
    return WorkflowSettings(
        definitions_dir=_resolve(root, discovery["definitions_dir"]),
        pattern=discovery.get("pattern", "*.workflow.yaml"),
        defaults=WorkflowDefaults(**doc.get("defaults", {})),
    )


def _evaluation(doc: Mapping[str, Any]) -> EvaluationConfig:
    return EvaluationConfig(
        pass_policy=doc.get("pass_policy", "all"),
        overall_threshold=doc.get("overall_threshold"),
        criteria=tuple(
            EvaluationCriterion(c["id"], c["description"], c["scorer"], float(c["threshold"]), float(c.get("weight", 1)))
            for c in doc["criteria"]
        ),
        targets=tuple(EvaluationTarget(t["type"], t["id"], tuple(t["criteria"])) for t in doc.get("targets", [])),
    )


def _security(doc: Mapping[str, Any]) -> SecurityConfig:
    tools = doc["tools"]
    redaction = doc.get("redaction", {})
    commands = doc.get("commands", {})
    shell = doc.get("shell", {})
    environment = doc.get("environment")

    def rules(items: list[Mapping[str, Any]]) -> tuple[CommandRule, ...]:
        return tuple(CommandRule(r["id"], r["regex"], r.get("description", "")) for r in items)

    shell_policy = ShellPolicy(
        without_sandbox=shell.get("without_sandbox", "disable"),
        allowed_domains=tuple(shell.get("allowed_domains", ())),
        destructive_programs=tuple(shell.get("destructive_programs", DEFAULT_DESTRUCTIVE_PROGRAMS)),
        sensitive_variables=shell.get("sensitive_variables", DEFAULT_SENSITIVE_VARIABLES),
    )
    environment_policy = None if environment is None else EnvironmentPolicy(
        allow=tuple(environment.get("allow", ())), credentials=tuple(environment.get("credentials", ())))
    write_modes = dict(DEFAULT_WRITE_MODES)
    for mode, spec in (doc.get("write_modes") or {}).items():
        write_modes[mode] = ModeCapabilities(frozenset(spec["allow"]), spec.get("otherwise", "deny"),
                                             bool(spec.get("shell", True)))

    return SecurityConfig(
        denied_tools=tuple(tools.get("deny", ())),
        tool_overrides=tuple(ToolOverride(o["agent_id"], tuple(o.get("deny", ())), o.get("write_mode"))
                             for o in tools.get("agents", [])),
        write_modes=write_modes,
        denied_paths=tuple(doc.get("paths", {}).get("deny", ())),
        secret_patterns=tuple(SecretPattern(p["id"], p["regex"]) for p in doc.get("secrets", {}).get("patterns", [])),
        redaction_enabled=redaction.get("enabled", True),
        redaction_replacement=redaction.get("replacement", "[REDACTED]"),
        max_inline_bytes=doc["artifacts"]["max_inline_bytes"],
        denied_commands=rules(commands.get("deny", [])),
        approval_commands=rules(commands.get("require_approval", [])),
        shell=shell_policy,
        environment=environment_policy,
    )


# --------------------------------------------------------------------------- entry point


def resolve_profile(profile: str | None = None) -> str:
    """The explicit profile, else ``$DAE_HARNESS_PROFILE``, else ``full``. Unknown names are an error."""
    value = profile or os.environ.get(PROFILE_ENV) or DEFAULT_PROFILE
    if value not in PROFILES:
        raise ConfigError("Unknown harness profile", [ValidationIssue(
            "profile", "", f"unknown profile '{value}' (known: {', '.join(PROFILES)})", "unknown-profile")])
    return value


def _load_orchestration(root: Path, rel: str, issues: list[ValidationIssue]) -> "OrchestrationPolicy | None":
    from .orchestration import POLICY_KIND, OrchestrationPolicy

    path = _resolve(root, rel)
    label = relative_label(path, root)
    try:
        doc = load_yaml(path, label)
    except ConfigError as exc:
        issues.extend(exc.issues)
        return None
    found = check_document(doc, label, POLICY_KIND, "orchestration-policy")
    issues.extend(found)
    return None if found else OrchestrationPolicy.from_dict(doc, source=path)


def _apply_profile(config: HarnessConfig, profile: str) -> HarnessConfig:
    """``pack``: drop optional plugins and everything that belongs to them, recording each id."""
    if profile == DEFAULT_PROFILE:
        return config
    dropped = tuple(p for p in config.plugins if p.optional)
    ids = {p.id for p in dropped}
    agents = tuple(a for a in config.agents if a.plugin not in ids)
    skills = tuple(s for s in config.skills if s.plugin not in ids)
    gone_agents = {a.id for a in config.agents} - {a.id for a in agents}
    security = replace(config.security, tool_overrides=tuple(
        o for o in config.security.tool_overrides if o.agent_id not in gone_agents))
    evaluation = replace(config.evaluation, targets=tuple(
        t for t in config.evaluation.targets if not (t.type == "agent" and t.id in gone_agents)))
    excluded = ([f"plugin:{p.id}" for p in dropped] + [f"agent:{a}" for a in sorted(gone_agents)]
                + sorted(f"skill:{s.id}" for s in config.skills if s.plugin in ids))
    return replace(config, plugins=tuple(p for p in config.plugins if not p.optional), agents=agents, skills=skills,
                   security=security, evaluation=evaluation, profile=profile, excluded=tuple(excluded),
                   excluded_plugins=dropped)


def load_config(root: Path | str, framework_file: str = DEFAULT_FRAMEWORK_FILE, *,
                profile: str | None = None) -> HarnessConfig:
    """Load and validate every configuration file of the harness rooted at ``root``.

    ``profile`` (default ``$DAE_HARNESS_PROFILE``, else ``full``) selects whether optional plugins are
    excluded explicitly; see :data:`PROFILES`.
    """
    profile = resolve_profile(profile)
    root = Path(root).resolve()
    framework_path = _resolve(root, framework_file)
    framework_label = relative_label(framework_path, root)

    framework_doc = load_yaml(framework_path, framework_label)
    issues = check_document(framework_doc, framework_label, "FrameworkConfig", "framework-config")
    if issues:
        _raise(issues, "Invalid framework configuration")

    sources: dict[str, Path] = {"framework": framework_path}
    docs: dict[str, Mapping[str, Any]] = {}
    for key, (kind, schema_name) in _CONFIG_FILES.items():
        path = _resolve(root, framework_doc["configs"][key])
        label = relative_label(path, root)
        sources[key] = path
        try:
            doc = load_yaml(path, label)
        except ConfigError as exc:
            issues.extend(exc.issues)
            continue
        found = check_document(doc, label, kind, schema_name)
        if not found:
            found = _semantic_issues(key, doc, label)
        issues.extend(found)
        docs[key] = doc
    orchestration = None
    if framework_doc["configs"].get(ORCHESTRATION_KEY):
        sources[ORCHESTRATION_KEY] = _resolve(root, framework_doc["configs"][ORCHESTRATION_KEY])
        orchestration = _load_orchestration(root, framework_doc["configs"][ORCHESTRATION_KEY], issues)
    if issues:
        _raise(issues, "Invalid configuration")

    framework = _framework(framework_doc, root)
    plugins = tuple(_plugin(p, framework.repo_root) for p in docs["agent_registry"].get("plugins", []))
    config = HarnessConfig(
        framework=framework,
        agents=tuple(_agent(a, framework.repo_root, plugins) for a in docs["agent_registry"]["agents"]),
        skills=tuple(_skill(s, framework.repo_root, plugins) for s in docs["skill_registry"]["skills"]),
        workflow=_workflow(docs["workflow"], root),
        evaluation=_evaluation(docs["evaluation"]),
        security=_security(docs["security"]),
        sources=sources,
        plugins=plugins,
        orchestration=orchestration,
    )
    config = _apply_profile(config, profile)
    log.debug("config.loaded", extra={"root": str(root), "agents": len(config.agents), "skills": len(config.skills)})
    return config
