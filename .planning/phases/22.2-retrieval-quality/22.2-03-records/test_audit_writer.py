"""Tests for audit_writer.py, on hand-made transcript lines.

One test per rejection, and one accepted call in each Windows path form
(22.2-03-PLAN.md, "The audit"). Run from the repository root:

    python -m pytest .planning/phases/22.2-retrieval-quality/22.2-03-records/test_audit_writer.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import audit_writer  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]  # the session's working directory: this repository
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path forms")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    """A checkout (`linkwarden`) with one source file, and a sibling outside it."""
    root = tmp_path / "corpora" / "linkwarden"
    (root / "apps" / "web" / "lib").mkdir(parents=True)
    (root / "apps" / "web" / "lib" / "x.ts").write_text("export function f() {}\n")
    (root / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.ts").write_text("export const s = 1;\n")
    return root, outside


def line(name, args):
    return json.dumps({"type": "assistant", "isSidechain": True, "message": {
        "role": "assistant", "content": [{"type": "tool_use", "id": "toolu_x", "name": name, "input": args}]}})


def transcript(tmp_path, *calls, prompt="Write 15 questions.", agent_type="blind-question-writer"):
    """A transcript in Claude Code's subagent form, with its .meta.json."""
    lines = [json.dumps({"type": "user", "isSidechain": True, "message": {"role": "user", "content": prompt}})]
    lines += [line(n, a) for n, a in calls]
    lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                              "content": [{"type": "text", "text": "[]"}]}}))
    path = tmp_path / "agent-test.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if agent_type is not None:
        (tmp_path / "agent-test.meta.json").write_text(json.dumps({"agentType": agent_type}))
    return path


def run(tmp_path, root, *calls, **kw):
    return audit_writer.audit(transcript(tmp_path, *calls, **kw), root, "test")


def fwd(p):
    return str(p).replace("\\", "/")


def make_dir_link(link: Path, target: Path) -> None:
    """A directory link: a symlink where allowed, else (Windows) a junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Accepted: one call in each path form
# ---------------------------------------------------------------------------

def test_accepts_the_three_tools_inside_the_root(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root,
            ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")}),
            ("Grep", {"pattern": "f..unction", "path": fwd(root), "glob": "**/*.{ts,tsx}"}),
            ("Glob", {"pattern": "apps/**/*.ts", "path": fwd(root / "apps")}))
    assert not a.void, a.report()
    assert "BATCH: ACCEPTED" in a.report()
    assert a.agent_type == "blind-question-writer"
    assert a.prompt_sha256 is not None


@windows_only
def test_accepts_backslashes(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Read", {"file_path": str(root / "apps" / "web" / "lib" / "x.ts").replace("/", "\\")}))
    assert not a.void, a.report()


@windows_only
def test_accepts_forward_slashes_and_a_lower_case_drive(tmp_path, tree):
    root, _ = tree
    p = fwd(root / "apps/web/lib/x.ts")
    a = run(tmp_path, root, ("Read", {"file_path": p[0].lower() + p[1:]}))
    assert not a.void, a.report()


@windows_only
def test_accepts_git_bash_form(tmp_path, tree):
    root, _ = tree
    p = fwd(root / "apps/web/lib/x.ts")
    a = run(tmp_path, root, ("Grep", {"pattern": "f", "path": "/" + p[0].lower() + p[2:]}))
    assert not a.void, a.report()
    assert a.calls[0].normalised.startswith(p[0].upper() + ":/")


@windows_only
def test_accepts_long_path_prefix(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Glob", {"pattern": "**/*.ts", "path": "\\\\?\\" + str(root)}))
    assert not a.void, a.report()


@windows_only
def test_a_root_given_in_git_bash_form_is_the_same_root(tmp_path, tree):
    root, _ = tree
    r = fwd(root)
    a = audit_writer.audit(transcript(tmp_path, ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")})),
                           Path("/" + r[0].lower() + r[2:]), "test")
    assert not a.void, a.report()


# ---------------------------------------------------------------------------
# Void: one test per rejection
# ---------------------------------------------------------------------------

def test_void_a_call_into_the_sessions_working_directory(tmp_path, tree):
    """Reads in this repository need no permission prompt, so only the audit
    stops them."""
    root, _ = tree
    a = run(tmp_path, root,
            ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")}),
            ("Read", {"file_path": fwd(REPO_ROOT / "services/workers/scripts/rag_quality_harness.py")}))
    assert a.void
    assert not a.calls[0].reasons
    assert "outside the root" in a.calls[1].verdict
    assert "BATCH: VOID" in a.report()


def test_void_a_grep_of_the_sessions_working_directory(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "question", "path": fwd(REPO_ROOT)}))
    assert a.void and "outside the root" in a.calls[0].verdict


def test_void_a_path_outside_the_root(tmp_path, tree):
    root, outside = tree
    a = run(tmp_path, root, ("Read", {"file_path": fwd(outside / "secret.ts")}))
    assert a.void and "outside the root" in a.calls[0].verdict


def test_void_a_sibling_whose_name_extends_the_roots(tmp_path, tree):
    """`linkwarden-x` starts with `linkwarden` as a string, not as a segment."""
    root, _ = tree
    sibling = root.parent / "linkwarden-x"
    sibling.mkdir()
    a = run(tmp_path, root, ("Read", {"file_path": fwd(sibling / "y.ts")}))
    assert a.void and "outside the root" in a.calls[0].verdict


def test_void_a_symlink_inside_the_checkout_pointing_outside(tmp_path, tree):
    """The link's target, not its name, is what is checked."""
    root, outside = tree
    link = root / "apps" / "web" / "linked"
    make_dir_link(link, outside)
    a = run(tmp_path, root, ("Read", {"file_path": fwd(link / "secret.ts")}))
    assert a.void and "outside the root" in a.calls[0].verdict
    assert fwd(link) in a.links


def test_void_a_true_symlink_pointing_outside(tmp_path, tree):
    """The same with an OS symlink to a file and to a directory. On Windows
    creating one needs a privilege (or developer mode); without it this test
    is skipped and the junction above stands for the directory link."""
    root, outside = tree
    try:
        os.symlink(outside / "secret.ts", root / "apps" / "web" / "lib" / "s.ts")
        os.symlink(outside, root / "apps" / "web" / "d", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")
    a = run(tmp_path, root,
            ("Read", {"file_path": fwd(root / "apps/web/lib/s.ts")}),
            ("Read", {"file_path": fwd(root / "apps/web/d/secret.ts")}))
    assert a.void
    assert all("outside the root" in c.verdict for c in a.calls)


def test_void_a_search_over_a_directory_holding_a_link_out(tmp_path, tree):
    root, outside = tree
    make_dir_link(root / "apps" / "web" / "linked", outside)
    a = run(tmp_path, root,
            ("Grep", {"pattern": "s", "path": fwd(root / "apps")}),
            ("Glob", {"pattern": "**/*.ts", "path": fwd(root)}))
    assert a.void
    assert all("link out of the root" in c.verdict for c in a.calls)


def test_a_link_that_stays_inside_the_root_is_accepted(tmp_path, tree):
    root, _ = tree
    make_dir_link(root / "apps" / "alias", root / "apps" / "web")
    a = run(tmp_path, root, ("Read", {"file_path": fwd(root / "apps/alias/lib/x.ts")}),
            ("Grep", {"pattern": "f", "path": fwd(root)}))
    assert not a.void, a.report()


def test_void_a_relative_path(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Read", {"file_path": "apps/web/lib/x.ts"}))
    assert a.void and "relative" in a.calls[0].verdict


def test_void_a_relative_search_path(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "f", "path": "."}), ("Glob", {"pattern": "*.ts", "path": "apps"}))
    assert a.void and all("relative" in c.verdict for c in a.calls)


def test_void_a_dotdot_path_even_when_it_lands_inside(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Read", {"file_path": fwd(root) + "/apps/../apps/web/lib/x.ts"}))
    assert a.void and "'..' segment" in a.calls[0].verdict


def test_void_a_dotdot_path_leaving_the_root(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "s", "path": fwd(root) + "/../../outside"}))
    assert a.void
    assert "'..' segment" in a.calls[0].verdict and "outside the root" in a.calls[0].verdict


def test_void_a_grep_with_no_path(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "f"}))
    assert a.void and "no path" in a.calls[0].verdict


def test_void_a_glob_with_no_path(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Glob", {"pattern": "**/*.ts"}))
    assert a.void and "no path" in a.calls[0].verdict


def test_void_a_read_with_no_file_path(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Read", {}))
    assert a.void and "no file_path" in a.calls[0].verdict


@pytest.mark.parametrize("pattern", ["C:/Users/**/*.ts", "c:\\Users\\*.ts", "/c/Users/**/*.ts", "/etc/*", "~/*.ts",
                                     "\\\\?\\C:\\x\\*.ts"])
def test_void_an_absolute_glob_pattern(tmp_path, tree, pattern):
    root, _ = tree
    a = run(tmp_path, root, ("Glob", {"pattern": pattern, "path": fwd(root)}))
    assert a.void and "pattern is absolute" in a.calls[0].verdict


def test_void_a_dotdot_glob_pattern(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Glob", {"pattern": "../**/*.ts", "path": fwd(root / "apps")}))
    assert a.void and "'..' segment" in a.calls[0].verdict


@pytest.mark.parametrize("glob", ["C:/Users/**/*.ts", "/c/Users/*.ts", "\\\\?\\C:\\x\\*.ts"])
def test_void_an_absolute_grep_glob_filter(tmp_path, tree, glob):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "f", "path": fwd(root), "glob": glob}))
    assert a.void and "glob filter is absolute" in a.calls[0].verdict


@pytest.mark.parametrize("glob", ["../**/*.ts", "apps/..\\..\\*.ts"])
def test_void_a_dotdot_grep_glob_filter(tmp_path, tree, glob):
    root, _ = tree
    a = run(tmp_path, root, ("Grep", {"pattern": "f", "path": fwd(root), "glob": glob}))
    assert a.void and "glob filter has a '..' segment" in a.calls[0].verdict


@pytest.mark.parametrize("tool", ["Bash", "WebFetch", "Write", "Agent", "mcp__x__y"])
def test_void_any_other_tool(tmp_path, tree, tool):
    root, _ = tree
    a = run(tmp_path, root, (tool, {"command": "ls"}))
    assert a.void and f"tool {tool} is not one of" in a.calls[0].verdict


def test_void_another_agent_type(tmp_path, tree):
    root, _ = tree
    a = run(tmp_path, root, ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")}), agent_type="general-purpose")
    assert a.void and "general-purpose" in a.batch_reasons[0]


def test_one_bad_call_voids_the_whole_batch(tmp_path, tree):
    root, _ = tree
    good = ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")})
    a = run(tmp_path, root, good, good, ("Grep", {"pattern": "f"}), good)
    assert a.void
    assert [bool(c.reasons) for c in a.calls] == [False, False, True, False]


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_cli_exit_codes_and_appended_report(tmp_path, tree):
    root, outside = tree
    out = tmp_path / "writer-audit.txt"
    ok = transcript(tmp_path, ("Read", {"file_path": fwd(root / "apps/web/lib/x.ts")}))
    assert audit_writer.main(["--root", fwd(root), "--transcript", str(ok), "--label", "writer 1",
                              "--out", str(out)]) == 0
    bad = transcript(tmp_path, ("Read", {"file_path": fwd(outside / "secret.ts")}))
    assert audit_writer.main(["--root", fwd(root), "--transcript", str(bad), "--label", "writer 2",
                              "--out", str(out), "--append"]) == 1
    text = out.read_text(encoding="utf-8")
    assert "== writer 1" in text and "BATCH: ACCEPTED" in text
    assert "== writer 2" in text and "BATCH: VOID" in text


def test_cli_refuses_a_relative_root(tmp_path, tree):
    ok = transcript(tmp_path)
    assert audit_writer.main(["--root", "linkwarden", "--transcript", str(ok)]) == 2
