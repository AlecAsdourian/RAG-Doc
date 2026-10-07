"""Audit one blind writer's transcript: every tool call stays inside the root.

22.2-03-PLAN.md, "The audit". A writer (`.claude/agents/blind-question-writer.md`)
may use Read, Grep and Glob, on the pinned checkout only. Its tools are
restricted by the agent definition; this audit is the proof that it kept to
the checkout. It reads the agent's transcript (the JSONL Claude Code keeps per
subagent, `<project>/<session>/subagents/agent-<id>.jsonl`) and checks every
`tool_use` block in it.

Any one of these voids the writer's whole batch:
- a tool other than Read, Grep and Glob, except one `SubagentHandback`
  (amended 2026-10-06, the user's decision: writers are launched in the
  background from the planner's session, and the harness returns their
  answer through that call). It is allowed only when it is the final
  `tool_use` in the transcript and its input is exactly `{"message": <a
  string>}`, which names no path. A second hand-back, one that is not last,
  or one with any other key or a non-string message voids the batch;
- a Read with no `file_path`, or a Grep or Glob with no `path` (without one
  they search the session's working directory, which is this repository);
- a relative path anywhere (it resolves against the session's working
  directory, not the root);
- a path, a Glob pattern or a Grep `glob` filter with a `..` segment;
- a Glob pattern, or a Grep `glob` filter, that is itself absolute;
- a path outside the root once normalised and resolved with links followed,
  so a link inside the checkout is judged by its target, not its name;
- a Grep or Glob whose search directory holds a link that leads out of the
  root;
- a transcript launched as another agent type than `blind-question-writer`,
  when the transcript's `.meta.json` says which.

Paths are normalised before the check: the `\\\\?\\` long-path prefix removed,
backslashes to slashes, Git Bash's `/c/...` to `C:/...`, and the drive letter
upper-cased. They are then resolved (`os.path.realpath`, which follows symlinks
and junctions) and compared with the resolved root segment by segment,
ignoring case on Windows.

Grep's `pattern` is a regular expression, not a path, so `..` in it is not a
path segment and is not checked.

Usage (from the repository root):

    python .planning/phases/22.2-retrieval-quality/22.2-03-records/audit_writer.py \\
        --root C:/Users/Alec/Desktop/code/rag-bench-corpora/linkwarden \\
        --transcript <session>/subagents/agent-<id>.jsonl \\
        --label "writer 1" --out .../22.2-03-records/writer-audit.txt --append

Exit 0: the batch is accepted. Exit 1: the batch is void. Exit 2: the
transcript or the root could not be read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

ALLOWED_TOOLS = ("Read", "Grep", "Glob")
# A writer launched in the background (from the planner's session) hands its
# answer back through one call of this tool, which the harness adds. One is
# allowed, only as the final tool_use and only as {"message": <string>}
# (22.2-03-PLAN.md, revision notice of 2026-10-06).
HANDBACK_TOOL = "SubagentHandback"
AGENT_TYPE = "blind-question-writer"
WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def normalise(raw: str) -> str:
    """One spelling for every form a Windows path arrives in.

    `\\\\?\\C:\\x` and `\\\\?\\UNC\\host\\share` lose their long-path prefix;
    backslashes become slashes; on Windows, Git Bash's `/c/x` becomes `C:/x`;
    a drive letter is upper-cased."""
    s = raw.strip()
    for prefix, repl in (("\\\\?\\UNC\\", "//"), ("//?/UNC/", "//"), ("\\\\?\\", ""), ("//?/", "")):
        if s.startswith(prefix):
            s = repl + s[len(prefix):]
            break
    s = s.replace("\\", "/")
    if WINDOWS:
        m = re.match(r"^/([A-Za-z])(/|$)", s)
        if m:
            s = f"{m.group(1)}:/" + s[m.end():]
    if re.match(r"^[A-Za-z]:", s):
        s = s[0].upper() + s[1:]
    return s


def is_absolute(normalised: str) -> bool:
    if WINDOWS:
        return bool(re.match(r"^[A-Z]:/", normalised)) or normalised.startswith("//")
    return normalised.startswith("/")


def looks_absolute_pattern(normalised: str) -> bool:
    """A glob that names a place on its own: a drive, a root, a UNC share or
    a home directory. Checked on every platform, since a pattern is a string,
    not a path on this machine."""
    return bool(re.match(r"^([A-Za-z]:|/|~)", normalised))


def has_dotdot(normalised: str) -> bool:
    return ".." in normalised.split("/")


def resolve(normalised: str) -> str:
    """The real location, links followed, in normalised spelling."""
    return normalise(os.path.realpath(normalised))


def segments(path: str) -> List[str]:
    parts = [p for p in path.split("/") if p]
    if path.startswith("//"):
        parts = ["//"] + parts
    elif path.startswith("/"):
        parts = ["/"] + parts
    return [p.casefold() for p in parts] if WINDOWS else parts


def inside(resolved: str, root_resolved: str) -> bool:
    r, p = segments(root_resolved), segments(resolved)
    return len(p) >= len(r) and p[: len(r)] == r


def outward_links(root_resolved: str) -> List[str]:
    """Links (symlinks and junctions) under the root whose target is outside
    it, in normalised spelling. `.git` is not searched."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root_resolved, followlinks=False):
        if ".git" in dirnames and normalise(dirpath) == root_resolved:
            dirnames.remove(".git")
        for name in list(dirnames) + filenames:
            full = os.path.join(dirpath, name)
            is_link = os.path.islink(full) or (hasattr(os.path, "isjunction") and os.path.isjunction(full))
            if is_link and not inside(resolve(normalise(full)), root_resolved):
                found.append(normalise(full))
                if name in dirnames:
                    dirnames.remove(name)
    return sorted(found)


# ---------------------------------------------------------------------------
# The audit
# ---------------------------------------------------------------------------

@dataclass
class Call:
    line: int
    tool: str
    path_arg: Optional[str]
    pattern: Optional[str]
    glob: Optional[str]
    normalised: Optional[str] = None
    resolved: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "VOID: " + "; ".join(self.reasons) if self.reasons else "ok"


def check_path(call: Call, raw: Optional[str], root_resolved: str, what: str) -> None:
    if raw is None or not str(raw).strip():
        call.reasons.append(f"no {what} argument (it would default to the session's working directory)")
        return
    norm = normalise(str(raw))
    call.normalised = norm
    if not is_absolute(norm):
        call.reasons.append(f"relative {what} (it resolves against the session's working directory)")
        return
    if has_dotdot(norm):
        call.reasons.append(f"{what} has a '..' segment")
    res = resolve(norm)
    call.resolved = res
    if not inside(res, root_resolved):
        call.reasons.append(f"{what} resolves outside the root, to {res}")


def check_pattern(call: Call, raw: Optional[str], what: str) -> None:
    if raw is None:
        return
    norm = normalise(str(raw))
    if looks_absolute_pattern(norm):
        call.reasons.append(f"{what} is absolute")
    if has_dotdot(norm):
        call.reasons.append(f"{what} has a '..' segment")


def check_links(call: Call, links: Sequence[str]) -> None:
    if call.resolved is None:
        return
    below = [l for l in links if inside(l, call.resolved)]
    if below:
        call.reasons.append("its search directory holds a link out of the root: " + ", ".join(below))


def audit_call(line: int, name: str, args: dict, root_resolved: str, links: Sequence[str],
               is_last: bool = False) -> Call:
    """One call's verdict. `is_last`: whether it is the transcript's final
    tool_use, which only matters for the one hand-back allowed."""
    args = args if isinstance(args, dict) else {}
    if name == "Read":
        call = Call(line, name, args.get("file_path"), None, None)
        check_path(call, args.get("file_path"), root_resolved, "file_path")
    elif name == "Grep":
        call = Call(line, name, args.get("path"), args.get("pattern"), args.get("glob"))
        check_path(call, args.get("path"), root_resolved, "path")
        check_pattern(call, args.get("glob"), "glob filter")
        check_links(call, links)
    elif name == "Glob":
        call = Call(line, name, args.get("path"), args.get("pattern"), None)
        check_path(call, args.get("path"), root_resolved, "path")
        if args.get("pattern") is None:
            call.reasons.append("no pattern")
        check_pattern(call, args.get("pattern"), "pattern")
        check_links(call, links)
    elif name == HANDBACK_TOOL:
        call = Call(line, name, None, None, None)
        if not is_last:
            call.reasons.append(f"{HANDBACK_TOOL} is not the final tool call")
        if set(args) != {"message"}:
            call.reasons.append(f"{HANDBACK_TOOL} input keys are {sorted(args)}, not exactly ['message']")
        elif not isinstance(args["message"], str):
            call.reasons.append(f"{HANDBACK_TOOL} message is not a string")
    else:
        call = Call(line, name, None, None, None)
        call.reasons.append(f"tool {name} is not one of {', '.join(ALLOWED_TOOLS)}")
    return call


def tool_uses(lines: Iterable[str]):
    """(line number, tool name, input) for every tool_use block, in order."""
    for n, text in enumerate(lines, 1):
        text = text.strip()
        if not text:
            continue
        entry = json.loads(text)
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield n, str(block.get("name")), block.get("input")


def first_prompt(lines: Sequence[str]) -> Optional[str]:
    """The text the agent was launched with: the first user message."""
    for text in lines:
        if not text.strip():
            continue
        entry = json.loads(text)
        if entry.get("type") != "user":
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return None


@dataclass
class Audit:
    label: str
    transcript: str
    root_resolved: str
    agent_type: Optional[str]
    prompt_sha256: Optional[str]
    links: List[str]
    calls: List[Call]
    batch_reasons: List[str]

    @property
    def void(self) -> bool:
        return bool(self.batch_reasons) or any(c.reasons for c in self.calls)

    def report(self) -> str:
        out = [
            f"== {self.label}",
            f"transcript: {self.transcript}",
            f"root (resolved): {self.root_resolved}",
            f"agent type: {self.agent_type or 'not recorded (no .meta.json beside the transcript)'}",
            f"launch prompt sha256: {self.prompt_sha256 or 'not found'}",
            "links out of the root under it: " + (", ".join(self.links) if self.links else "none"),
        ]
        for r in self.batch_reasons:
            out.append(f"VOID (batch): {r}")
        for i, c in enumerate(self.calls, 1):
            out.append(
                f"{i:3d}. line {c.line} {c.tool}"
                f" path={json.dumps(c.path_arg)} pattern={json.dumps(c.pattern)} glob={json.dumps(c.glob)}"
                f" normalised={json.dumps(c.normalised)} resolved={json.dumps(c.resolved)} -> {c.verdict}")
        per_tool = {t: sum(c.tool == t for c in self.calls) for t in sorted({c.tool for c in self.calls})}
        bad = sum(bool(c.reasons) for c in self.calls)
        out.append(f"calls: {len(self.calls)} {json.dumps(per_tool)}; void calls: {bad}")
        out.append(f"BATCH: {'VOID' if self.void else 'ACCEPTED'}")
        return "\n".join(out) + "\n"


def audit(transcript: Path, root: Path, label: str = "writer") -> Audit:
    root_norm = normalise(str(root))
    if not is_absolute(root_norm):
        raise ValueError(f"the root must be absolute: {root}")
    root_resolved = resolve(root_norm)
    if not os.path.isdir(root_resolved):
        raise ValueError(f"the root is not a directory: {root_resolved}")
    lines = transcript.read_text(encoding="utf-8").splitlines()
    links = outward_links(root_resolved)
    uses = list(tool_uses(lines))
    calls = [audit_call(n, name, args, root_resolved, links, is_last=(i == len(uses) - 1))
             for i, (n, name, args) in enumerate(uses)]

    batch_reasons = []
    agent_type = None
    meta = transcript.with_name(transcript.name[: -len(".jsonl")] + ".meta.json") if transcript.name.endswith(".jsonl") else None
    if meta is not None and meta.exists():
        agent_type = json.loads(meta.read_text(encoding="utf-8")).get("agentType")
        if agent_type != AGENT_TYPE:
            batch_reasons.append(f"launched as agent type {agent_type!r}, not {AGENT_TYPE!r}")
    prompt = first_prompt(lines)
    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt is not None else None
    return Audit(label, str(transcript), root_resolved, agent_type, sha, links, calls, batch_reasons)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="the pinned checkout, as an absolute path")
    ap.add_argument("--transcript", required=True, help="the writer agent's JSONL transcript")
    ap.add_argument("--label", default="writer", help="how the report names this run (e.g. 'writer 1')")
    ap.add_argument("--out", help="write the report here (default: stdout only)")
    ap.add_argument("--append", action="store_true", help="append to --out instead of replacing it")
    a = ap.parse_args(argv)
    try:
        result = audit(Path(a.transcript), Path(a.root), a.label)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"audit_writer: {exc}", file=sys.stderr)
        return 2
    text = result.report()
    sys.stdout.write(text)
    if a.out:
        with open(a.out, "a" if a.append else "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return 1 if result.void else 0


if __name__ == "__main__":
    sys.exit(main())
