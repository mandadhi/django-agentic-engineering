"""Static analysis of a shell command line for security policy. Standard library only, Python 3.8+.

Shared, byte for byte, by the harness guard (``agent_runtime/security_hooks.py``, which imports it
from ``core``) and by the standalone plugin's guard (``hooks/shell_syntax.py``, a generated copy that
the packaging check compares with this file). Each guard applies its own policy to the facts
returned here; this module decides nothing about roots, modes or approvals.

It is a real lexer and recursive-descent parser for the POSIX shell (plus the bash forms agents
use), not a set of regular expressions:

* quoting: ``'...'``, ``"..."``, ``$'...'`` (escapes decoded), ``$"..."``, backslashes, line
  continuations, comments;
* every redirection form: ``[n]>``, ``[n]>>``, ``[n]>|``, ``&>``, ``&>>``, ``[n]<``, ``[n]<>``,
  ``[n]>&word``, ``[n]<&word``, here-documents and here-strings (fd duplication ``2>&1`` and
  closing ``>&-`` are recognised as such, never as files);
* lists, pipelines, subshells, brace groups, ``if``/``for``/``while`` keywords;
* expansions: ``$VAR``, ``${...}``, ``$(...)``, backticks, ``$((...))``, ``<(...)``/``>(...)``.
  Command and process substitutions are parsed recursively, so the commands inside them are
  analysed like any other.

:func:`analyze` also unwraps commands that run other commands: ``sh/bash/zsh -c``, ``eval``,
``env``/``sudo``/``nohup``/``timeout``/``xargs``/... wrappers and ``find -exec``. Inline code for
other interpreters (``python -c``, ``node -e``, ``powershell -Command``, ...) cannot be analysed
and is reported as *opaque*. Anything the parser cannot analyse with certainty is reported as a
problem, so callers fail closed.

Effects (SEC-06). Every simple command, nested and unwrapped ones included, is classified by what
it does, not by its name alone: ``read``, ``write``, ``delete``, ``execute``, ``network`` or
``unknown`` (:attr:`Facts.effects`). Classification is an allow-list: a command is ``read`` only when
its program *and* its options are known to be read-only (``git status`` but not ``git -c ...``,
``sort`` but not ``sort -o``, ``sed`` without ``-i`` or a ``w``/``e`` command, ``manage.py check``);
everything not recognised is ``unknown``. Wrappers carry their own effect (``sudo`` is ``execute``)
and the command they run is classified separately. Output redirections are reported in
:attr:`Facts.writes`, not as effects.
"""

from __future__ import annotations

import glob as _glob
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__all__ = ["ShellSyntaxError", "Word", "Redirect", "Command", "Facts", "parse", "analyze", "expand_word",
           "classify", "CAPABILITIES", "READ", "WRITE", "DELETE", "EXECUTE", "NETWORK", "UNKNOWN"]

#: Effect classes of a command (SEC-06). Write modes grant a subset; ``unknown`` is never implied.
READ, WRITE, DELETE, EXECUTE, NETWORK, UNKNOWN = "read", "write", "delete", "execute", "network", "unknown"
CAPABILITIES = (READ, WRITE, DELETE, EXECUTE, NETWORK, UNKNOWN)

MAX_DEPTH = 8                       # nested shells / substitutions analysed before giving up (fail closed)
NULL_DEVICES = frozenset({"/dev/null", "nul", "NUL", "$null", "/dev/stdout", "/dev/stderr"})
_OPERATORS = ("&&", "||", ";;", "|&", ";", "&", "|", "(", ")", "\n")
_REDIRECT_OPS = ("&>>", "&>", ">>", ">|", ">&", "<<<", "<<-", "<<", "<>", "<&", ">", "<")
_WRITE_OPS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_RESERVED = frozenset({"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "!", "{", "}",
                       "time", "case", "esac", "select", "function"})
_SPECIAL_PARAMS = "?#@*$!-0123456789"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Programs that run their arguments as another command (after their own options).
_WRAPPERS = frozenset({"env", "sudo", "doas", "nice", "nohup", "time", "timeout", "exec", "command", "builtin",
                       "stdbuf", "ionice", "setsid", "chrt", "taskset", "xargs", "watch", "strace", "ltrace",
                       "unbuffer", "caffeinate", "flock", "chronic", "runuser", "su"})
#: Wrapper options that consume the following word as their value.
_WRAPPER_VALUE_OPTS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user", "--group", "--chdir"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "xargs": {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "--arg-file", "--delimiter", "--max-args",
              "--max-procs", "--max-chars", "--replace"},
    "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"}, "watch": {"-n", "-d", "--interval"},
    "flock": {"-w", "-E"}, "chrt": set(), "taskset": set(),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish", "busybox"})
#: Interpreters whose inline code cannot be analysed, and the options that introduce it.
_INLINE_CODE = {
    "python": {"-c"}, "python2": {"-c"}, "python3": {"-c"}, "py": {"-c"}, "pypy": {"-c"}, "pypy3": {"-c"},
    "node": {"-e", "--eval", "-p", "--print"}, "deno": {"eval"}, "bun": {"-e", "--eval"},
    "perl": {"-e", "-E"}, "ruby": {"-e"}, "php": {"-r"}, "lua": {"-e"}, "rscript": {"-e"},
    "osascript": {"-e"}, "powershell": {"-c", "-command", "-encodedcommand", "-ec", "-e"},
    "pwsh": {"-c", "-command", "-encodedcommand", "-ec", "-e"}, "cmd": {"/c", "/k", "/r"},
}
_AWK = frozenset({"awk", "gawk", "mawk", "nawk"})
#: Programs whose first operand is a pattern or script, not a path.
_PATTERN_FIRST = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "jq", "yq", *_AWK})
_PATTERN_OPTS = frozenset({"-e", "--regexp", "-f", "--file", "--expression", "-E"})
#: Programs whose operands are printed, never opened.
_NO_PATH_OPERANDS = frozenset({"echo", "printf", "true", "false", ":", "sleep", "seq", "expr", "date", "basename",
                               "dirname"})
_ENV_DUMP = frozenset({"printenv", "compgen"})


class ShellSyntaxError(ValueError):
    """The command cannot be parsed with certainty; callers must treat it as unsafe."""


# ---------------------------------------------------------------------- syntax tree


@dataclass
class Word:
    """One shell word. ``parts`` keep how each piece was written: ('lit'|'quoted'|'expansion', text)."""

    parts: List[Tuple[str, str]] = field(default_factory=list)
    variables: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(text for _, text in self.parts)

    @property
    def dynamic(self) -> bool:
        """Its value depends on a parameter expansion or a command substitution."""
        return any(kind == "expansion" for kind, _ in self.parts)

    @property
    def glob(self) -> bool:
        return any(kind == "lit" and any(c in text for c in "*?[") for kind, text in self.parts)

    @property
    def brace(self) -> bool:
        literal = "".join(text if kind == "lit" else "\0" for kind, text in self.parts)
        return bool(re.search(r"\{[^{}\0]*(,|\.\.)[^{}\0]*\}", literal))

    @property
    def tilde(self) -> bool:
        return bool(self.parts) and self.parts[0][0] == "lit" and self.parts[0][1].startswith("~")

    def __repr__(self) -> str:   # pragma: no cover - debugging aid
        return f"Word({self.text!r})"


@dataclass
class Redirect:
    op: str
    fd: Optional[int]
    target: Word

    @property
    def is_fd_dup(self) -> bool:
        """``2>&1``, ``>&-``, ``<&0``: duplicates or closes a descriptor, opens no file."""
        return self.op in (">&", "<&") and bool(re.fullmatch(r"\d+-?|-", self.target.text)) \
            and not self.target.dynamic

    @property
    def writes(self) -> bool:
        return self.op in _WRITE_OPS or (self.op == ">&" and not self.is_fd_dup)

    @property
    def reads(self) -> bool:
        return self.op in ("<", "<>") or (self.op == "<&" and not self.is_fd_dup)


@dataclass
class Command:
    argv: List[Word] = field(default_factory=list)
    assignments: List[Word] = field(default_factory=list)       # values of NAME=value prefixes
    redirects: List[Redirect] = field(default_factory=list)
    depth: int = 0
    names: List[str] = field(default_factory=list)              # names of NAME=value prefixes
    via: str = ""                  # the wrapper that runs this command ('xargs', 'find': extra operands)

    @property
    def program(self) -> str:
        return os.path.basename(self.argv[0].text).lower() if self.argv else ""

    @property
    def normalized(self) -> str:
        """The command as the shell would see it after quote removal (for rule matching)."""
        return " ".join(w.text for w in self.argv)


# ---------------------------------------------------------------------- lexer + parser


class _Parser:
    def __init__(self, text: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ShellSyntaxError("commands are nested too deeply to analyse")
        self.s = text
        self.i = 0
        self.depth = depth
        self.commands: List[Command] = []
        self._heredocs: List[Tuple[str, bool]] = []

    # -- helpers
    def _peek(self, n: int = 0) -> str:
        j = self.i + n
        return self.s[j] if j < len(self.s) else ""

    def _startswith(self, token: str) -> bool:
        return self.s.startswith(token, self.i)

    def _skip_blanks(self) -> None:
        while True:
            c = self._peek()
            if c in (" ", "\t", "\r"):
                self.i += 1
            elif c == "\\" and self._peek(1) == "\n":
                self.i += 2
            elif c == "#":
                while self._peek() not in ("", "\n"):
                    self.i += 1
            else:
                return

    def _nested(self, text: str) -> None:
        self.commands.extend(_Parser(text, self.depth + 1).parse())

    # -- grammar: list := (command | '(' list ')' | operator)*
    def parse(self, until: str = "") -> List[Command]:
        current = Command(depth=self.depth)
        expect_command = True
        while True:
            self._skip_blanks()
            c = self._peek()
            if c == "":
                if until:
                    raise ShellSyntaxError(f"missing '{until}'")
                break
            if until and c == until:
                self.i += 1
                break
            if c == "\n" and self._heredocs:
                self.i += 1
                self._read_heredoc_bodies()
                self._close(current)
                current, expect_command = Command(depth=self.depth), True
                continue
            if self._startswith("((") and expect_command and not current.argv:
                self.i += 2
                self._skip_balanced("((", "))", kind="arithmetic")
                continue
            op = None if self._startswith("&>") else next((o for o in _OPERATORS if self._startswith(o)), None)
            if op is not None:
                if op == "(":
                    if current.argv:
                        raise ShellSyntaxError("unexpected '(' (function definitions are not analysed)")
                    self.i += 1
                    self.parse(until=")")
                    continue
                if op == ")":
                    raise ShellSyntaxError("unbalanced ')'")
                if op == ";;":
                    raise ShellSyntaxError("case statements are not analysed")
                self.i += len(op)
                self._close(current)
                current, expect_command = Command(depth=self.depth), True
                continue
            redirect = self._redirect()
            if redirect is not None:
                current.redirects.append(redirect)
                continue
            word = self._word()
            if expect_command and not current.argv and _is_assignment(word):
                current.assignments.append(_assignment_value(word))
                current.names.append(re.match(r"[A-Za-z_][A-Za-z0-9_]*", word.parts[0][1]).group())
                continue
            if expect_command and not current.argv and word.text in _RESERVED and not word.dynamic:
                continue                                  # keywords introduce the command that follows
            current.argv.append(word)
            expect_command = False
        self._close(current)
        if self._heredocs:
            raise ShellSyntaxError("here-document without a body")
        return self.commands

    def _close(self, command: Command) -> None:
        if command.argv or command.assignments or command.redirects:
            self.commands.append(command)

    def _redirect(self) -> Optional[Redirect]:
        start = self.i
        fd: Optional[int] = None
        digits = re.match(r"\d+", self.s[self.i:])
        if digits:
            after = self.i + digits.end()
            if not any(self.s.startswith(o, after) for o in _REDIRECT_OPS) or self.s.startswith("&>", after):
                return None
            fd = int(digits.group())
            self.i = after
        op = next((o for o in _REDIRECT_OPS if self._startswith(o)), None)
        if op is None:
            self.i = start
            return None
        if op in ("<", ">") and self._peek(1) == "(":
            if fd is not None:
                raise ShellSyntaxError("descriptor before a process substitution")
            self.i = start
            return None                                   # <(...) / >(...): part of a word
        self.i += len(op)
        self._skip_blanks()
        if self._peek() in ("", "\n", ";", "|", "&", ")") and op not in ("<<", "<<-"):
            raise ShellSyntaxError(f"redirection '{op}' without a target")
        target = self._word()
        if op in ("<<", "<<-"):
            quoted = any(kind == "quoted" for kind, _ in target.parts) or "\\" in self.s[start:self.i]
            self._heredocs.append((target.text, quoted))
        return Redirect(op, fd, target)

    def _read_heredoc_bodies(self) -> None:
        for delimiter, quoted in self._heredocs:
            lines = []
            while True:
                end = self.s.find("\n", self.i)
                line = self.s[self.i:] if end < 0 else self.s[self.i:end]
                self.i = len(self.s) if end < 0 else end + 1
                if line.lstrip("\t") == delimiter:
                    break
                lines.append(line)
                if end < 0:
                    raise ShellSyntaxError(f"here-document '{delimiter}' is not terminated")
            if not quoted:      # expansions and substitutions run in the body: analyse it as a "..." word
                body = _Parser('"' + "\n".join(lines).replace('"', "'") + '"', self.depth + 1)
                body._word()
                self.commands.extend(body.commands)
        self._heredocs = []

    # -- words
    def _word(self) -> Word:
        word = Word()
        while True:
            c = self._peek()
            if c == "" or c in " \t\r\n;&|()":
                break
            if c in "<>":
                if self._peek(1) == "(":                  # process substitution
                    self.i += 2
                    self.parse(until=")")
                    word.parts.append(("expansion", f"{c}(...)"))
                    continue
                break
            if c == "'":
                end = self.s.find("'", self.i + 1)
                if end < 0:
                    raise ShellSyntaxError("unterminated single quote")
                word.parts.append(("quoted", self.s[self.i + 1:end]))
                self.i = end + 1
            elif c == '"':
                self.i += 1
                self._double_quoted(word)
            elif c == "\\":
                nxt = self._peek(1)
                self.i += 2
                if nxt == "\n":
                    continue
                if nxt == "":
                    raise ShellSyntaxError("trailing backslash")
                word.parts.append(("quoted", nxt))
            elif c == "$":
                self._dollar(word, in_double=False)
            elif c == "`":
                self._backtick(word)
            else:
                start = self.i
                while self._peek() and self._peek() not in " \t\r\n;&|()<>'\"\\$`":
                    self.i += 1
                word.parts.append(("lit", self.s[start:self.i]))
        if not word.parts:
            raise ShellSyntaxError(f"unexpected character {self._peek()!r}")
        return word

    def _double_quoted(self, word: Word) -> None:
        buf = []
        while True:
            c = self._peek()
            if c == "":
                raise ShellSyntaxError("unterminated double quote")
            if c == '"':
                self.i += 1
                break
            if c == "\\" and self._peek(1) in ('"', "\\", "$", "`", "\n"):
                if self._peek(1) != "\n":
                    buf.append(self._peek(1))
                self.i += 2
            elif c in "$`":
                if buf:
                    word.parts.append(("quoted", "".join(buf)))
                    buf = []
                if c == "$":
                    self._dollar(word, in_double=True)
                else:
                    self._backtick(word)
            else:
                buf.append(c)
                self.i += 1
        word.parts.append(("quoted", "".join(buf)))

    def _dollar(self, word: Word, *, in_double: bool) -> None:
        nxt = self._peek(1)
        if nxt == "'" and not in_double:
            self.i += 2
            word.parts.append(("quoted", self._ansi_c()))
        elif nxt == '"' and not in_double:
            self.i += 2
            self._double_quoted(word)
        elif self.s.startswith("$((", self.i):
            self.i += 3
            self._skip_balanced("((", "))", kind="arithmetic")
            word.parts.append(("expansion", "$((...))"))
        elif nxt == "(":
            self.i += 2
            self.parse(until=")")
            word.parts.append(("expansion", "$(...)"))
        elif nxt == "{":
            self.i += 2
            start = self.i
            self._skip_balanced("{", "}", kind="parameter expansion")
            inner = self.s[start:self.i - 1]         # substitutions inside were refused by _skip_balanced
            name = re.match(r"[!#]?([A-Za-z_][A-Za-z0-9_]*|[" + re.escape(_SPECIAL_PARAMS) + "])", inner)
            word.variables.append(name.group(1) if name else inner)
            word.parts.append(("expansion", "${" + inner + "}"))
        else:
            match = _NAME.match(self.s, self.i + 1)
            if match:
                self.i = match.end()
                word.variables.append(match.group())
                word.parts.append(("expansion", "$" + match.group()))
            elif nxt and nxt in _SPECIAL_PARAMS:
                self.i += 2
                word.variables.append(nxt)
                word.parts.append(("expansion", "$" + nxt))
            else:
                self.i += 1
                word.parts.append(("quoted" if in_double else "lit", "$"))

    def _backtick(self, word: Word) -> None:
        self.i += 1
        buf = []
        while True:
            c = self._peek()
            if c == "":
                raise ShellSyntaxError("unterminated backtick")
            if c == "`":
                self.i += 1
                break
            if c == "\\" and self._peek(1) in ("`", "\\", "$"):
                buf.append(self._peek(1))
                self.i += 2
            else:
                buf.append(c)
                self.i += 1
        self._nested("".join(buf))
        word.parts.append(("expansion", "`...`"))

    def _skip_balanced(self, opening: str, closing: str, *, kind: str) -> None:
        level = 1
        while level:
            if self.i >= len(self.s):
                raise ShellSyntaxError(f"unterminated {kind}")
            if self._startswith(closing):
                level -= 1
                self.i += len(closing)
                continue
            if self._startswith(opening):
                level += 1
                self.i += len(opening)
                continue
            if self._startswith("$(") or self._peek() == "`":
                raise ShellSyntaxError(f"command substitution inside {kind} is not analysed")
            self.i += 1

    def _ansi_c(self) -> str:
        out = []
        simple = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                  "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
        while True:
            c = self._peek()
            if c == "":
                raise ShellSyntaxError("unterminated $'...' string")
            self.i += 1
            if c == "'":
                return "".join(out)
            if c != "\\":
                out.append(c)
                continue
            e = self._peek()
            self.i += 1
            if e in simple:
                out.append(simple[e])
            elif e in "01234567":
                digits = re.match(r"[0-7]{1,2}", self.s[self.i:])
                text = e + (digits.group() if digits else "")
                self.i += len(text) - 1
                out.append(chr(int(text, 8)))
            elif e in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[e]
                digits = re.match("[0-9A-Fa-f]{1,%d}" % width, self.s[self.i:])
                if not digits:
                    out.append("\\" + e)
                    continue
                self.i += digits.end()
                out.append(chr(int(digits.group(), 16)))
            elif e == "c" and self._peek():
                out.append(chr(ord(self._peek()) & 0x1F))
                self.i += 1
            else:
                out.append("\\" + e)


def _is_assignment(word: Word) -> bool:
    return bool(word.parts) and word.parts[0][0] == "lit" and bool(re.match(r"[A-Za-z_][A-Za-z0-9_]*\+?=",
                                                                            word.parts[0][1]))


def _assignment_value(word: Word) -> Word:
    kind, text = word.parts[0]
    value = Word(parts=[(kind, text.split("=", 1)[1])] + word.parts[1:], variables=list(word.variables))
    value.parts = [p for p in value.parts if p[1] or p[0] != "lit"]
    return value


def parse(command: str, depth: int = 0) -> List[Command]:
    """Every simple command in ``command``, including those inside substitutions, in source order."""
    if "\x00" in command:
        raise ShellSyntaxError("NUL byte in command")
    return _Parser(command, depth).parse()


# ---------------------------------------------------------------------- analysis


@dataclass
class Facts:
    """What a command line does, as far as static analysis can tell."""

    commands: List[Command] = field(default_factory=list)       # every simple command, nested ones included
    problems: List[str] = field(default_factory=list)           # cannot be analysed safely -> deny
    path_words: List[Word] = field(default_factory=list)        # operands that name files or directories
    writes: List[Word] = field(default_factory=list)            # output redirection targets (not null devices)
    reads: List[Word] = field(default_factory=list)             # input redirection sources
    destructive: List[str] = field(default_factory=list)        # deletes or destroys files
    opaque: List[str] = field(default_factory=list)             # runs code that cannot be analysed
    env_dump: List[str] = field(default_factory=list)           # lists environment variables
    variables: List[str] = field(default_factory=list)          # every expanded parameter name
    effects: List[Tuple[str, str]] = field(default_factory=list)  # (capability, command) per simple command

    def capabilities(self) -> "set[str]":
        """Every effect class the command line has (redirections are in :attr:`writes`)."""
        return {capability for capability, _ in self.effects}


def analyze(command: str, *, destructive_programs=("rm", "rmdir", "unlink", "shred", "truncate", "srm", "wipe"),
            ) -> Facts:
    facts = Facts()
    try:
        queue = list(parse(command))
    except ShellSyntaxError as exc:
        facts.problems.append(f"the command cannot be analysed ({exc})")
        return facts
    destructive = {p.lower() for p in destructive_programs}
    seen = 0
    while queue:
        cmd = queue.pop(0)
        seen += 1
        if seen > 500:
            facts.problems.append("the command is too complex to analyse")
            break
        facts.commands.append(cmd)
        for word in [*cmd.argv, *cmd.assignments, *(r.target for r in cmd.redirects)]:
            facts.variables.extend(word.variables)
        for redirect in cmd.redirects:
            if redirect.op in ("<<", "<<-", "<<<") or redirect.is_fd_dup:
                continue
            target = redirect.target
            if target.dynamic or target.glob or target.brace:
                facts.problems.append(f"redirection target '{target.text}' depends on an expansion")
            elif target.text not in NULL_DEVICES:
                (facts.writes if redirect.writes else facts.reads).append(target)
        for value in cmd.assignments:
            if _path_like(value):
                facts.path_words.append(value)
        if not cmd.argv:
            # NAME=value alone changes the shell for every later command (PATH, BASH_ENV, ...)
            facts.effects.extend((_env_effect(name), f"{name}=...") for name in cmd.names)
            continue
        program = cmd.program
        if cmd.argv[0].dynamic:
            facts.problems.append("the program name depends on an expansion")
            continue
        facts.effects.extend((capability, cmd.normalized) for capability in classify(cmd, destructive))
        inner = _unwrap(cmd, facts, destructive, queue)
        if inner is _STOP:
            continue
        _operands(cmd, program, facts)
        if program in destructive:
            facts.destructive.append(cmd.normalized)
        if program == "find":
            _find(cmd, facts, destructive, queue)
        if program in ("cd", "pushd", "chdir") and (len(cmd.argv) == 1 or cmd.argv[1].text in ("-", "~")):
            facts.problems.append("'cd' without a directory inside the workspace")
        if program in _ENV_DUMP or (program in ("export", "declare", "typeset", "set", "readonly")
                                    and (len(cmd.argv) == 1 or cmd.argv[1].text in ("-p", "-x", "-px"))):
            facts.env_dump.append(cmd.normalized)
    return facts


_STOP = object()


def _unwrap(cmd: Command, facts: Facts, destructive, queue: List[Command]):
    """Queue the command a wrapper, shell or ``eval`` runs. Returns _STOP when ``cmd`` is fully handled."""
    program, argv = cmd.program, cmd.argv
    if program in _WRAPPERS:
        value_opts = _WRAPPER_VALUE_OPTS.get(program, set())
        i = 1
        while i < len(argv):
            text = argv[i].text
            if program == "env" and "=" in text and not text.startswith("-"):
                if _path_like(argv[i]):
                    facts.path_words.append(argv[i])
                i += 1
                continue
            if program == "timeout" and i == 1 and re.fullmatch(r"[\d.]+[smhd]?", text):
                i += 1
                continue
            if program in ("nice", "ionice", "chrt", "taskset", "flock") and re.fullmatch(r"-?[\d,.]+", text):
                i += 1
                continue
            if program == "su":
                facts.opaque.append(cmd.normalized)
                return _STOP
            if program == "command" and text in ("-v", "-V"):
                return _STOP                                 # looks a name up; runs nothing
            if text.startswith("-"):
                if text in value_opts:                       # -a FILE, -C DIR, ...: the value may be a path
                    if i + 1 < len(argv) and _path_like(argv[i + 1]):
                        facts.path_words.append(argv[i + 1])
                    i += 2
                    continue
                if "=" in text:
                    value = Word(parts=[("lit", text.split("=", 1)[1])], variables=list(argv[i].variables))
                    if _path_like(value):
                        facts.path_words.append(value)
                i += 1
                continue
            break
        if i < len(argv):
            queue.insert(0, Command(argv=argv[i:], depth=cmd.depth + 1, via=program))
        elif program == "env":
            facts.env_dump.append(cmd.normalized)
        return _STOP
    if program in _SHELLS:
        for i, word in enumerate(argv[1:], start=1):
            text = word.text
            if text.startswith("-") and not text.startswith("--") and "c" in text[1:]:
                if i + 1 >= len(argv):
                    facts.problems.append(f"'{program} -c' without a command")
                elif argv[i + 1].dynamic:
                    facts.problems.append(f"'{program} -c' runs a command built from an expansion")
                else:
                    _queue_text(argv[i + 1].text, cmd, facts, queue)
                return _STOP
            if not text.startswith("-"):
                facts.opaque.append(cmd.normalized)                   # runs a script file
                return _STOP
        facts.opaque.append(cmd.normalized)                           # reads commands from stdin
        return _STOP
    if program in ("eval", "source", "."):
        if program == "eval":
            if any(w.dynamic for w in argv[1:]):
                facts.problems.append("'eval' of an expansion cannot be analysed")
            else:
                _queue_text(" ".join(w.text for w in argv[1:]), cmd, facts, queue)
        else:
            facts.opaque.append(cmd.normalized)
        return _STOP
    key = program[:-4] if program.endswith(".exe") else program
    key = re.sub(r"^(python)\d+(\.\d+)?$", r"\1", key)
    if key in _INLINE_CODE:
        flags = {f.lower() for f in _INLINE_CODE[key]}
        if any(w.text.lower() in flags or (key.startswith("python") and w.text.startswith("-c"))
               for w in argv[1:]):
            facts.opaque.append(cmd.normalized)
            return None
    if key in _AWK:
        program_text = next((w.text for w in argv[1:] if not w.text.startswith("-")), "")
        if re.search(r"\bsystem\s*\(|\|\s*\"|>\s*\"|\bgetline\b", program_text):
            facts.opaque.append(cmd.normalized)
    return None


def _queue_text(text: str, cmd: Command, facts: Facts, queue: List[Command]) -> None:
    try:
        nested = parse(text, cmd.depth + 1)
    except ShellSyntaxError as exc:
        facts.problems.append(f"a nested command cannot be analysed ({exc})")
        return
    queue[0:0] = nested


def _find(cmd: Command, facts: Facts, destructive, queue: List[Command]) -> None:
    argv = cmd.argv
    i = 1
    while i < len(argv):
        text = argv[i].text
        if text == "-delete":
            facts.destructive.append(cmd.normalized)
        if text in ("-exec", "-execdir", "-ok", "-okdir"):
            end = next((j for j in range(i + 1, len(argv)) if argv[j].text in (";", "+")), len(argv))
            if end == i + 1:
                facts.problems.append("'find -exec' without a command")
            else:
                queue.insert(0, Command(argv=argv[i + 1:end], depth=cmd.depth + 1, via="find"))
            i = end
        if text in ("-fprint", "-fprint0", "-fprintf", "-fls") and i + 1 < len(argv):
            facts.writes.append(argv[i + 1])
        i += 1


def _operands(cmd: Command, program: str, facts: Facts) -> None:
    """Collect the words that name paths: operands and ``--opt=path`` / ``-o/path`` values."""
    if program in _NO_PATH_OPERANDS:
        return
    argv = cmd.argv
    pattern_pending = program in _PATTERN_FIRST
    key = re.sub(r"^(python)\d+(\.\d+)?$", r"\1", program[:-4] if program.endswith(".exe") else program)
    code_flags = {f.lower() for f in _INLINE_CODE.get(key, ())}
    skip_next = False
    end_of_options = False
    for word in argv[1:]:
        text = word.text
        if skip_next:
            skip_next = False
            continue
        if text.lower() in code_flags:
            skip_next = True                                  # inline code: reported as opaque, not a path
            continue
        if text in ("<(...)", ">(...)"):
            continue                                          # process substitution: its commands are analysed
        if any(kind == "expansion" and value in ("$(...)", "`...`") for kind, value in word.parts):
            facts.problems.append(f"operand '{word.text}' is built from command output that cannot be verified")
            continue
        if program == "find" and text in ("-exec", "-execdir", "-ok", "-okdir"):
            break                                            # the -exec command is analysed on its own
        if not end_of_options and text == "--":
            end_of_options = True
            continue
        if not end_of_options and text.startswith("-") and len(text) > 1:
            if program in _PATTERN_FIRST and text in _PATTERN_OPTS:
                pattern_pending = False                      # the pattern comes with this option
                skip_next = True
                continue
            if "=" in text:
                value = Word(parts=[("lit", text.split("=", 1)[1])], variables=list(word.variables))
                if word.dynamic:
                    value.parts.append(("expansion", ""))
                if _path_like(value):
                    facts.path_words.append(value)
            elif not text.startswith("--") and len(text) > 2 and text[2] in "/~." and program not in _PATTERN_FIRST:
                facts.path_words.append(Word(parts=[("lit", text[2:])]))
            continue
        if pattern_pending:
            pattern_pending = False
            continue
        if _URL.match(text) or _REMOTE.match(text):
            continue
        if _path_like(word) or word.glob or word.brace:
            facts.path_words.append(word)


_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_REMOTE = re.compile(r"^[\w.-]+@[\w.-]+:")


def _path_like(word: Word) -> bool:
    text = word.text
    if not text or _URL.match(text) or _REMOTE.match(text):
        return False
    return (text.startswith(("/", "~", "./", "../", ".\\", "..\\")) or text in (".", "..")
            or "/" in text or "\\" in text or bool(re.match(r"^[A-Za-z]:", text)) or word.tilde
            or text.startswith("."))


def expand_word(word: Word, cwd: str, home: str) -> Tuple[List[str], Optional[str]]:
    """Literal paths ``word`` stands for: brace expansion, ``~``, then glob matches under ``cwd``.

    Returns (paths, problem). A word that depends on an expansion has no static value: problem.
    A glob that matches nothing is passed to the program unchanged by the shell, so the literal is
    returned. Paths are as the shell would pass them (relative to ``cwd`` when relative).
    """
    if word.dynamic:
        return [], f"'{word.text}' depends on a shell expansion that cannot be verified"
    texts = [word.text]
    if word.brace:
        if any(kind != "lit" for kind, _ in word.parts):
            return [], f"'{word.text}' mixes quoting and brace expansion"
        texts = _brace_expand(word.text)
        if texts is None:
            return [], f"'{word.text}' uses a brace expansion that cannot be verified"
    out: List[str] = []
    for text in texts:
        if word.tilde or text.startswith("~"):
            if text == "~" or text.startswith(("~/", "~\\")):
                text = home + text[1:]
            else:
                return [], f"'{text}' names another user's home directory"
        if word.glob:
            pattern = text if os.path.isabs(text) else os.path.join(cwd, text)
            matches = sorted(_glob.glob(pattern, recursive=True))
            if len(matches) > 5000:
                return [], f"'{text}' matches too many files to verify"
            out.extend(matches or [text])
        else:
            out.append(text)
    return out, None


def _brace_expand(text: str) -> Optional[List[str]]:
    match = re.search(r"\{([^{}]*)\}", text)
    if not match or ("," not in match.group(1) and ".." not in match.group(1)):
        return [text]
    body = match.group(1)
    if ".." in body and "," not in body:
        bounds = body.split("..")
        if len(bounds) not in (2, 3) or not all(re.fullmatch(r"-?\d+|[A-Za-z]", b) for b in bounds[:2]):
            return None
        if bounds[0].lstrip("-").isdigit():
            a, b = int(bounds[0]), int(bounds[1])
            items = [str(n) for n in (range(a, b + 1) if a <= b else range(a, b - 1, -1))]
        else:
            a, b = ord(bounds[0]), ord(bounds[1])
            items = [chr(n) for n in (range(a, b + 1) if a <= b else range(a, b - 1, -1))]
        if len(items) > 1000:
            return None
    else:
        items = body.split(",")
    results: List[str] = []
    for item in items:
        expanded = _brace_expand(text[:match.start()] + item + text[match.end():])
        if expanded is None:
            return None
        results.extend(expanded)
        if len(results) > 5000:
            return None
    return results


# ---------------------------------------------------------------------- effects (SEC-06)
#
# An allow-list: a command is READ only when its program and options are known not to change
# anything. Every other program is UNKNOWN, so a write mode that grants only READ refuses it.

#: Variables a command may set without changing which programs run or where they look for code
#: or configuration (PATH, LD_*, BASH_ENV, PYTHONPATH, GIT_*, HOME, ... are not on it).
_SAFE_ENV = re.compile(r"^(LANG|LANGUAGE|LC_[A-Z]+|TZ|TERM|COLUMNS|LINES|NO_COLOR|FORCE_COLOR|CLICOLOR|CI|"
                       r"PYTHONDONTWRITEBYTECODE|PYTHONIOENCODING|PYTHONUTF8|PYTHONUNBUFFERED|PYTHONHASHSEED|"
                       r"DJANGO_SETTINGS_MODULE|GIT_OPTIONAL_LOCKS)$")
#: Programs that only read files or print, whatever their options.
_READ_PROGRAMS = frozenset({
    "cat", "tac", "head", "tail", "wc", "ls", "dir", "vdir", "file", "stat", "du", "df", "pwd", "echo", "printf",
    "true", "false", "test", "[", "[[", ":", "basename", "dirname", "realpath", "readlink", "which", "whereis",
    "type", "nl", "cut", "tr", "column", "paste", "join", "comm", "cmp", "diff", "md5sum", "sha1sum", "sha224sum",
    "sha256sum", "sha384sum", "sha512sum", "b2sum", "cksum", "sum", "base32", "base64", "od", "hexdump",
    "strings", "fold", "fmt", "expand", "unexpand", "rev", "grep", "egrep", "fgrep", "ag", "ack", "jq",
    "uname", "whoami", "id", "groups", "uptime", "ps", "sleep", "seq", "expr", "cd", "pushd", "popd", "dirs",
    "unset", "read", "getconf", "locale", "nproc", "arch", "numfmt", "tsort", "factor", "wait", "set",
})
_WRITE_PROGRAMS = frozenset({
    "cp", "mv", "mkdir", "touch", "ln", "install", "patch", "chmod", "chown", "chgrp", "dd", "tee", "split",
    "csplit", "mktemp", "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz", "zstd", "7z",
    "mkfifo", "mknod", "chattr", "setfacl",
})
_NETWORK_PROGRAMS = frozenset({
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "http", "https",
    "aria2c", "socat", "ping", "dig", "nslookup", "host",
})
#: Wrappers that only run the command after them (classified on its own). Others are EXECUTE.
_TRANSPARENT_WRAPPERS = frozenset({"env", "nice", "time", "timeout", "command", "builtin", "exec", "stdbuf", "xargs"})
_PY_FLAGS = frozenset({"-B", "-E", "-I", "-s", "-S", "-u", "-O", "-OO", "-q", "-b", "-bb", "-R"})
_DJANGO_READ = frozenset({"check", "showmigrations", "sqlmigrate", "help", "version"})
_DJANGO_WRITE = frozenset({"migrate", "flush", "loaddata", "dumpdata", "createsuperuser", "changepassword",
                           "collectstatic", "startapp", "startproject", "makemessages", "compilemessages",
                           "createcachetable", "squashmigrations", "clearsessions", "sqlflush", "optimizemigration"})
_DJANGO_EXECUTE = frozenset({"test", "shell", "dbshell", "runserver", "testserver"})
_GIT_READ = frozenset({"status", "diff", "log", "show", "blame", "annotate", "shortlog", "describe", "rev-parse",
                       "rev-list", "ls-files", "ls-tree", "cat-file", "merge-base", "name-rev", "for-each-ref",
                       "show-ref", "count-objects", "check-ignore", "check-attr", "grep", "whatchanged",
                       "show-branch", "cherry", "range-diff", "diff-tree", "diff-files", "diff-index", "version",
                       "help", "var", "verify-commit", "verify-tag"})
_GIT_DELETE = frozenset({"rm", "clean"})
_GIT_NETWORK = frozenset({"fetch", "pull", "push", "clone", "ls-remote", "send-email", "request-pull"})
_GIT_WRITE = frozenset({"add", "commit", "checkout", "switch", "reset", "restore", "merge", "rebase", "cherry-pick",
                        "revert", "apply", "am", "mv", "init", "gc", "prune", "repack", "update-ref", "update-index",
                        "write-tree", "commit-tree", "mktag", "read-tree", "checkout-index", "bisect",
                        "sparse-checkout", "maintenance", "filter-branch", "replace", "format-patch", "archive",
                        "fsck", "pack-refs", "mailinfo", "stage", "citool", "difftool", "mergetool"})
#: git options that run programs taken from the command line or configuration.
_GIT_EXEC_OPTIONS = ("-c", "--config-env", "--exec-path", "-p", "--paginate")
_GIT_VALUE_OPTIONS = ("-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix")
_GIT_FLAG_OPTIONS = frozenset({"--no-pager", "-P", "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs",
                               "--noglob-pathspecs", "--icase-pathspecs", "--bare", "--no-optional-locks",
                               "--no-lazy-fetch", "--version", "--help", "-h"})


def _env_effect(name: str) -> str:
    return READ if _SAFE_ENV.match(name) else UNKNOWN


def _positionals(args: List[str], value_options=()) -> List[str]:
    """Non-option words (after ``--`` every word), skipping the values of ``value_options``."""
    out, skip, ended = [], False, False
    for arg in args:
        if skip:
            skip = False
            continue
        if not ended and arg == "--":
            ended = True
            continue
        if not ended and arg.startswith("-") and len(arg) > 1:
            skip = arg in value_options
            continue
        out.append(arg)
    return out


def _has(args: List[str], *options: str) -> bool:
    """An option is present: exactly, as ``--long=value`` or as ``-xVALUE``."""
    for arg in args:
        for option in options:
            if arg == option or (option.startswith("--") and arg.startswith(option + "=")) \
                    or (not option.startswith("--") and len(option) == 2 and arg.startswith(option)
                        and not arg.startswith("--")):
                return True
    return False


def classify(cmd: Command, destructive=frozenset({"rm", "rmdir", "unlink", "shred", "truncate", "srm", "wipe"}),
             ) -> List[str]:
    """The effect classes of one simple command (see the module docstring). Never empty."""
    program = cmd.program
    key = program[:-4] if program.endswith(".exe") else program
    key = re.sub(r"^(python)\d+(\.\d+)?$", r"\1", key)
    args = [w.text for w in cmd.argv[1:]]
    effects = [_env_effect(name) for name in cmd.names] + _program_effects(key, args, cmd, destructive)
    return list(dict.fromkeys(effects))


def _program_effects(key: str, args: List[str], cmd: Command, destructive) -> List[str]:
    if key in destructive:
        return [DELETE]
    if key in _WRAPPERS:
        if key not in _TRANSPARENT_WRAPPERS:
            return [EXECUTE]                                 # sudo, nohup, setsid, strace, flock, ...
        if key == "env":
            if _has(args, "-S", "--split-string"):
                return [UNKNOWN]                             # the split string is a command line nobody analysed
            names = [a.split("=", 1)[0] for a in args if "=" in a and not a.startswith("-")]
            return [_env_effect(name) for name in names] or [READ]
        return [READ]
    if key in _SHELLS:
        for arg in args:
            if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
                return [READ]                                # the -c command line is analysed on its own
            if not arg.startswith("-"):
                break
        return [EXECUTE]                                     # a script file or commands from stdin
    if key == "eval":
        return [READ]
    if key in ("source", "."):
        return [EXECUTE]
    if key in ("python", "py", "pypy", "pypy3"):
        return _python(args)
    if key in _INLINE_CODE and any(a.lower() in {f.lower() for f in _INLINE_CODE[key]} for a in args):
        return [EXECUTE]
    if key in ("manage.py", "django-admin", "django-admin.py"):
        return _django(args)
    if key in ("pip", "pip3"):
        return _pip(args)
    if key == "bandit":
        return [WRITE] if _has(args, "-o", "--output") else [READ]
    if key == "git":
        return _git(args)
    if key == "sed":
        return _sed(args)
    if key in _AWK:
        return _awk(args)
    if key == "find":
        effects = [READ]
        if "-delete" in args:
            effects.append(DELETE)
        if any(a in ("-fprint", "-fprint0", "-fprintf", "-fls") for a in args):
            effects.append(WRITE)
        return effects                                       # a -exec command is classified on its own
    if key == "sort":
        if _has(args, "--compress-program"):
            return [EXECUTE]
        if _has(args, "--output", "--temporary-directory"):
            return [WRITE]
        for arg in args:
            if arg.startswith("-") and not arg.startswith("--"):
                for letter in arg[1:]:
                    if letter in "oT":
                        return [WRITE]
                    if letter in "ktS":
                        break                                # the rest of the word is that option's value
        return [READ]
    if key in ("uniq", "xxd"):
        values = ("-f", "-s", "-w") if key == "uniq" else ("-c", "-cols", "-g", "-groupsize", "-l", "-len", "-s",
                                                           "-seek", "-o", "-offset", "-n", "-name")
        if cmd.via in ("xargs", "find"):
            return [UNKNOWN]                                 # extra operands would become the output file
        return [WRITE] if len(_positionals(args, values)) > 1 else [READ]
    if key == "tree":
        return [WRITE] if "-o" in args else [READ]
    if key == "rg":
        return [EXECUTE] if _has(args, "--pre") else [READ]
    if key in ("iconv", "sdiff"):
        return [WRITE] if _has(args, "-o", "--output") else [READ]
    if key == "yq":
        return [WRITE] if _has(args, "-i", "--inplace") else [READ]
    if key == "date":
        return [EXECUTE] if _has(args, "-s", "--set") else [READ]
    if key == "hostname":
        return [EXECUTE] if _positionals(args) else [READ]
    if key in ("export", "declare", "typeset", "local", "readonly"):
        effects = []
        for arg in args:
            if arg.startswith("-"):
                if "f" in arg[1:]:
                    return [UNKNOWN]                         # functions run code later
                continue
            effects.append(_env_effect(arg.split("=", 1)[0]))
        return effects or [READ]
    if key == "curl":
        return [NETWORK, WRITE] if _has(args, "-o", "-O", "--output", "--remote-name", "-D", "--dump-header",
                                        "-c", "--cookie-jar") else [NETWORK]
    if key == "wget":
        return [NETWORK, WRITE]
    if key in _READ_PROGRAMS:
        return [READ]
    if key in _WRITE_PROGRAMS:
        return [WRITE]
    if key in _NETWORK_PROGRAMS:
        return [NETWORK]
    return [UNKNOWN]


def _python(args: List[str]) -> List[str]:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-V", "--version", "-VV", "-h", "--help"):
            return [READ]
        if arg.startswith("-c"):
            return [EXECUTE]
        if arg == "-m":
            return _python_module(args[i + 1], args[i + 2:]) if i + 1 < len(args) else [UNKNOWN]
        if arg.startswith("-m"):
            return _python_module(arg[2:], args[i + 1:])
        if arg in ("-W", "-X"):
            i += 2
            continue
        if arg.startswith(("-W", "-X")) or arg in _PY_FLAGS:
            i += 1
            continue
        if arg.startswith("-"):
            return [EXECUTE] if arg == "-" else [UNKNOWN]
        if os.path.basename(arg).lower() == "manage.py":
            return _django(args[i + 1:])
        return [EXECUTE]                                     # runs a script
    return [EXECUTE]                                         # reads a program from stdin


def _python_module(module: str, args: List[str]) -> List[str]:
    if module == "pip":
        return _pip(args)
    if module == "bandit":
        return [WRITE] if _has(args, "-o", "--output") else [READ]
    if module == "django":
        return _django(args)
    if module == "json.tool":
        return [WRITE] if len(_positionals(args, ("--indent",))) > 1 else [READ]
    if module in ("tabnanny", "platform", "sysconfig"):
        return [READ]
    return [EXECUTE]                                         # runs that module's code


def _django(args: List[str]) -> List[str]:
    """``manage.py`` / ``django-admin`` subcommands. ``check`` and friends import the project's code."""
    positionals = _positionals(args, ("--settings", "--pythonpath", "-v", "--verbosity", "--database"))
    sub = positionals[0] if positionals else None
    if sub is None or sub in _DJANGO_READ or "-h" in args or "--help" in args:
        return [READ]
    if sub == "makemigrations":
        return [READ] if "--dry-run" in args else [WRITE]
    if sub in _DJANGO_WRITE:
        return [WRITE]
    if sub in _DJANGO_EXECUTE:
        return [EXECUTE]
    return [UNKNOWN]                                         # project-defined commands: anything


def _pip(args: List[str]) -> List[str]:
    positionals = _positionals(args, ("--python", "--log", "--cache-dir", "--proxy", "--retries", "--timeout",
                                      "--cert", "--client-cert", "--exists-action", "--trusted-host", "-r",
                                      "--requirement", "-c", "--constraint", "-i", "--index-url", "-f",
                                      "--find-links", "--extra-index-url", "--path", "--format", "--exclude"))
    sub = positionals[0] if positionals else None
    if sub is None:
        return [READ] if _has(args, "-V", "--version", "-h", "--help") or not args else [UNKNOWN]
    if sub in ("list", "show", "freeze", "check", "inspect", "help", "debug", "hash"):
        return [NETWORK] if sub == "list" and _has(args, "-o", "--outdated", "-u", "--uptodate") else [READ]
    if sub == "config":
        return [READ] if len(positionals) > 1 and positionals[1] in ("list", "get", "debug") else [WRITE]
    if sub in ("download", "install"):
        return [NETWORK, WRITE]
    if sub in ("index", "search"):
        return [NETWORK]
    if sub in ("uninstall", "wheel", "cache"):
        return [WRITE]
    return [UNKNOWN]


def _git(args: List[str]) -> List[str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if any(arg == o or arg.startswith(o + "=") for o in _GIT_EXEC_OPTIONS):
            return [EXECUTE]                                 # -c alias.x='!cmd', core.pager, ... run programs
        if arg in _GIT_VALUE_OPTIONS:
            i += 2
            continue
        if any(arg.startswith(o + "=") for o in _GIT_VALUE_OPTIONS if o.startswith("--")) or arg in _GIT_FLAG_OPTIONS:
            i += 1
            continue
        return [UNKNOWN]
    if i >= len(args):
        return [READ] if any(a in ("--version", "--help", "-h") for a in args) else [UNKNOWN]
    sub, rest = args[i], args[i + 1:]
    if sub in _GIT_READ:
        if _has(rest, "--output"):
            return [WRITE]
        if _has(rest, "--ext-diff", "--open-files-in-pager") or (sub == "grep" and _has(rest, "-O")):
            return [EXECUTE]
        return [READ]
    if sub == "branch":
        return _git_listing(rest, ("-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u",
                                   "--set-upstream-to", "--unset-upstream", "--edit-description", "-f", "--force",
                                   "-t", "--track", "--no-track", "--create-reflog"))
    if sub == "tag":
        return _git_listing(rest, ("-d", "--delete", "-a", "--annotate", "-s", "--sign", "-u", "--local-user",
                                   "-f", "--force", "-m", "--message", "-F", "--file", "-e", "--edit"))
    positionals = _positionals(rest)
    action = positionals[0] if positionals else None
    if sub == "stash":
        return [READ] if action in ("list", "show") else [WRITE]
    if sub == "remote":
        if action in (None, "get-url"):
            return [READ]
        if action == "show":
            return [READ] if "-n" in rest else [NETWORK]
        return [NETWORK] if action in ("update", "prune") else [WRITE]
    if sub == "config":
        if action in ("get", "list"):
            return [READ]
        if action in ("set", "unset", "rename-section", "remove-section", "edit", "unset-all", "replace-all") \
                or _has(rest, "--unset", "--unset-all", "--add", "--replace-all", "--rename-section",
                        "--remove-section", "-e", "--edit"):
            return [WRITE]
        reading = _has(rest, "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l")
        return [READ] if reading or len(positionals) <= 1 else [WRITE]
    if sub == "reflog":
        return [WRITE] if action in ("expire", "delete") else [READ]
    if sub in ("worktree", "notes"):
        return [READ] if action in ("list", "show") else [WRITE]
    if sub == "submodule":
        if action in ("status", "summary"):
            return [READ]
        return [EXECUTE] if action == "foreach" else [NETWORK, WRITE]
    if sub == "symbolic-ref":
        return [WRITE] if len(positionals) > 1 or _has(rest, "-d", "--delete") else [READ]
    if sub in _GIT_DELETE:
        return [DELETE]
    if sub in _GIT_NETWORK:
        return [NETWORK] if sub == "ls-remote" else [NETWORK, WRITE]
    if sub in _GIT_WRITE:
        return [WRITE]
    return [UNKNOWN]                                         # aliases and extensions run anything


def _git_listing(rest: List[str], writing) -> List[str]:
    """``git branch`` / ``git tag``: listing reads; creating, deleting, moving or editing writes."""
    if _has(rest, *writing):
        return [WRITE]
    listing = _has(rest, "-l", "--list")
    positionals = _positionals(rest, ("--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
                                      "--sort", "--format", "--color", "--column"))
    return [WRITE] if positionals and not listing else [READ]


def _sed(args: List[str]) -> List[str]:
    scripts: List[str] = []
    script_file = in_place = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and arg != "--":
            if arg == "--in-place" or arg.startswith("--in-place="):
                in_place = True
            elif arg in ("--expression", "--file", "--line-length"):
                if arg == "--expression" and i + 1 < len(args):
                    scripts.append(args[i + 1])
                script_file = script_file or arg == "--file"
                i += 1
            elif arg.startswith("--expression="):
                scripts.append(arg.split("=", 1)[1])
            elif arg.startswith("--file="):
                script_file = True
            elif arg not in ("--quiet", "--silent", "--regexp-extended", "--posix", "--separate", "--sandbox",
                             "--null-data", "--zero-terminated", "--debug", "--unbuffered", "--binary"):
                return [UNKNOWN]
        elif arg.startswith("-") and len(arg) > 1 and arg != "--":
            for j, letter in enumerate(arg[1:], start=2):
                if letter == "i":
                    in_place = True
                    break                                    # the rest is the backup suffix
                if letter in "efl":
                    value = arg[j:] or (args[i + 1] if i + 1 < len(args) else "")
                    if not arg[j:]:
                        i += 1
                    if letter == "e":
                        scripts.append(value)
                    script_file = script_file or letter == "f"
                    break
                if letter not in "nrEsuzb":
                    return [UNKNOWN]
        elif not scripts and not script_file:
            scripts.append(arg)                              # the first operand is the script
        i += 1
    if in_place:
        return [WRITE]
    if script_file or not scripts:
        return [UNKNOWN]                                     # a script file may contain w, e or r commands
    return list(dict.fromkeys(_sed_script(s) for s in scripts))


def _sed_script(s: str) -> str:
    """READ, or the first command that writes (``w``, ``s///w``), executes (``e``) or reads a named file."""
    n, i, result = len(s), 0, READ

    def skip_to(i: int, delimiter: str) -> int:
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == delimiter:
                return i + 1
            if s[i] == "\n":
                break
            i += 1
        raise ValueError("unterminated")

    try:
        while i < n:
            if s[i] in " \t\n;":
                i += 1
                continue
            for _ in range(2):                               # up to two addresses
                if s[i].isdigit() or s[i] == "$":
                    while i < n and (s[i].isdigit() or s[i] in "$~+"):
                        i += 1
                elif s[i] == "/":
                    i = skip_to(i + 1, "/")
                    while i < n and s[i] in "IM":
                        i += 1
                elif s[i] == "\\":
                    i = skip_to(i + 2, s[i + 1])
                    while i < n and s[i] in "IM":
                        i += 1
                else:
                    break
                if i < n and s[i] == ",":
                    i += 1
                    continue
                break
            while i < n and s[i] in " \t!":
                i += 1
            if i >= n:
                break
            command = s[i]
            i += 1
            if command in "{}=dDpPnNgGhHxzF":
                continue
            if command in "sy":
                delimiter = s[i]
                if delimiter in "\\\n":
                    return UNKNOWN
                i = skip_to(skip_to(i + 1, delimiter), delimiter)
                while command == "s" and i < n and s[i].isalnum():
                    if s[i] in "wW":
                        return WRITE                         # s///w FILE
                    if s[i] == "e":
                        result = EXECUTE
                    i += 1
                continue
            if command in "wW":
                return WRITE
            if command == "e":
                return EXECUTE
            if command in "rRaic:btTv":
                if command in "rR" and result == READ:
                    result = UNKNOWN                         # reads a file no path check has seen
                stop = "\n" if command in "rRaic" else ";\n"
                while i < n and s[i] not in stop:
                    i += 1
                continue
            if command in "qQlL":
                while i < n and s[i] in " \t0123456789":
                    i += 1
                continue
            return UNKNOWN
    except (ValueError, IndexError):
        return UNKNOWN
    return result


def _awk(args: List[str]) -> List[str]:
    program = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-f", "--file", "-i", "--include", "-l", "--load", "-E", "--exec") \
                or arg.startswith(("--file=", "--include=", "--load=", "--exec=")):
            return [EXECUTE]                                 # program files, extensions, in-place editing
        if arg in ("-e", "--source", "--"):
            program = args[i + 1] if i + 1 < len(args) else None
            break
        if arg in ("-v", "--assign", "-F", "--field-separator"):
            i += 2
            continue
        if arg.startswith("-"):
            if not (arg.startswith(("-v", "-F", "--assign=", "--field-separator=")) or arg in (
                    "-b", "-c", "-P", "-r", "-S", "--sandbox", "-t", "-n", "-O", "-M", "--posix", "--traditional")):
                return [UNKNOWN]
            i += 1
            continue
        program = arg
        break
    if program is None:
        return [UNKNOWN]
    if re.search(r"\bsystem\s*\(|(?<!\|)\|(?!\|)|\bgetline\b|@include|@load|\bENVIRON\b|\bPROCINFO\b", program):
        return [EXECUTE]                                     # commands, coprocesses, other files, the environment
    if re.search(r"\bprintf?\b[^;{}\n]*>", program):
        return [WRITE]                                       # print > file (also a comparison inside print)
    return [READ]
