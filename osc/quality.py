"""AI-slop linter: deterministic, repo-relative checks that catch the patterns reviewers
associate with machine-generated code. Every rule compares the *added* lines against a
baseline measured from the touched files / the repo's own tests, so a repo that writes
assert messages or docstrings everywhere is not penalised for matching its own style.

Output: {"score": 0-100, "findings": [{rule, severity, file, line, text}], "baselines": {...}}
Score 100 = nothing found. Severity: high (blocks), medium, low (advisory).
"""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from pathlib import Path

UNICODE_DASH = re.compile("[—–]")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿✅❌⭐]")
NARRATION = re.compile(r"^\s*(#|//|/\*|\*)\s*(this (function|method|class|block|code|section)|here we|now we|first,|then,|finally,|next,|"
                       r"note that|make sure|ensure (that|the)|important:|added|new:|updated|fix:|handle the|check if|"
                       r"we (need|want|use|check|call|return)|loop (over|through)|iterate|initialize|return the|call the|"
                       r"create (a|the)|get the|set the|define|step \d)", re.I)
BUZZWORDS = re.compile(r"\b(robust(ly|ness)?|comprehensive(ly)?|seamless(ly)?|leverag(e|es|ing)|enhanced?|gracefully|elegant(ly)?|"
                       r"streamlin(e|ed)|delve|utiliz(e|es|ing)|crucial|vital|cutting[- ]edge|state[- ]of[- ]the[- ]art|"
                       r"in order to|it is worth noting|it's worth noting|as an ai|ensures? (that )?(the )?(correct|proper)|"
                       r"best practices?|for clarity|for readability|for better|improved? (readability|maintainability)|"
                       r"this (ensures|guarantees|allows|helps|makes sure))\b", re.I)
PR_FILLER = re.compile(r"(let me know|feel free|i hope this helps|happy to (help|assist)|please let me know|"
                       r"don't hesitate|as requested|i have (implemented|added|created|updated)|this (pr|pull request|commit) (adds|implements|introduces|fixes)|"
                       r"^#+ .*(summary|overview|changes made|key changes|testing performed)\s*$)", re.I | re.M)
SEPARATOR = re.compile(r"^\s*(#|//)\s*[-=*#]{6,}\s*$")
BROAD_EXCEPT = re.compile(r"except\s*(Exception|BaseException)?\s*:\s*$")
DOCSTRING_SECTIONS = re.compile(r"^\s*(Args|Arguments|Returns|Raises|Parameters|Yields|Examples?):\s*$")
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".c", ".cc", ".cpp", ".cu", ".h", ".hpp", ".java", ".kt", ".swift", ".rb"}
COMMENT_START = {".py": "#", ".rb": "#", ".rs": "//", ".go": "//", ".ts": "//", ".tsx": "//", ".js": "//", ".jsx": "//",
                 ".c": "//", ".cc": "//", ".cpp": "//", ".cu": "//", ".h": "//", ".hpp": "//", ".java": "//", ".kt": "//", ".swift": "//"}


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True).stdout


def parse_diff(diff: str) -> dict[str, dict]:
    """{file: {"added": [(new_lineno, text)], "removed": [text], "new_file": bool}}"""
    files, cur, new_n = {}, None, 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            name = line.split(" b/")[-1]
            cur = files[name] = {"added": [], "removed": [], "new_file": False}
        elif cur is None:
            continue
        elif line.startswith("new file"):
            cur["new_file"] = True
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            new_n = int(m.group(1)) if m else 0
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            cur["added"].append((new_n, line[1:])); new_n += 1
        elif line.startswith("-"):
            cur["removed"].append(line[1:])
        else:
            new_n += 1
    return files


def _is_comment(text: str, ext: str) -> bool:
    c = COMMENT_START.get(ext)
    return bool(c) and text.strip().startswith(c)


def _baseline_text(text: str, ext: str) -> dict:
    """Style baseline of the whole (post-change) file."""
    lines = text.splitlines()
    code = [l for l in lines if l.strip()]
    comments = [l for l in code if _is_comment(l, ext)]
    defs = [l for l in lines if re.match(r"\s*(async\s+)?def\s+\w+\(", l)] if ext == ".py" else []
    hinted = [l for l in defs if "->" in l or re.search(r"\w+\s*:\s*\w", l.split("(", 1)[-1])]
    priv_defs, priv_doc = 0, 0
    for i, l in enumerate(lines):
        if re.match(r"\s*def\s+_\w+\(", l):
            priv_defs += 1
            nxt = "".join(lines[i + 1:i + 3])
            if '"""' in nxt or "'''" in nxt:
                priv_doc += 1
    return {"comment_ratio": len(comments) / max(1, len(code)), "hint_ratio": len(hinted) / max(1, len(defs)),
            "private_docstring_ratio": priv_doc / max(1, priv_defs), "private_defs": priv_defs,
            "has_separators": any(SEPARATOR.match(l) for l in lines),
            "has_docstring_sections": any(DOCSTRING_SECTIONS.match(l) for l in lines),
            "has_broad_except": any(BROAD_EXCEPT.search(l) for l in lines)}


def _assert_nodes(text: str) -> list:
    """(lineno, has_msg, msg_text) for every assert statement, via ast so multi-line asserts count."""
    import ast
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            msg = ""
            if node.msg is not None:
                try:
                    msg = ast.unparse(node.msg)
                except Exception:
                    msg = "?"
            out.append((node.lineno, node.msg is not None, msg, isinstance(node.msg, ast.Constant)))
    return out


def _baseline_tests(repo: Path) -> dict:
    """How the repo's existing tests write asserts (message ratio)."""
    total = with_msg = const_msg = 0
    for p in list(repo.rglob("test_*.py"))[:400] + list(repo.rglob("*_test.py"))[:200]:
        if ".venv" in p.parts or "node_modules" in p.parts:
            continue
        try:
            for _, has_msg, _, is_const in _assert_nodes(p.read_text(errors="ignore")):
                total += 1
                with_msg += has_msg
                const_msg += is_const
        except Exception:
            pass
    return {"assert_msg_ratio": with_msg / max(1, total), "asserts_seen": total,
            "const_msg_ratio": const_msg / max(1, with_msg)}


def _duplicate_blocks(added: list[tuple[int, str]], full_text: str, min_len: int = 3) -> list[tuple[int, int]]:
    """Added runs of >= min_len non-trivial lines that also occur elsewhere in the file."""
    norm = [l.strip() for l in full_text.splitlines()]
    added_set = {n for n, _ in added}
    out, i = [], 0
    seq = [(n, t.strip()) for n, t in added if t.strip() and not t.strip().startswith(("#", "//", ")", "]", "}"))]
    while i + min_len <= len(seq):
        block = [t for _, t in seq[i:i + min_len]]
        if all(len(t) > 3 for t in block) and sum(len(t) for t in block) >= 45:
            for j in range(len(norm) - min_len + 1):
                if (j + 1) in added_set:
                    continue
                if norm[j:j + min_len] == block:
                    out.append((seq[i][0], j + 1)); i += min_len; break
            else:
                i += 1
        else:
            i += 1
    return out


def lint_change(repo_path: Path, base_sha: str, head_sha: str, pr_title: str = "", pr_body: str = "",
                commit_msgs: str | None = None, ai_policy: str = "") -> dict:
    diff = _run(["git", "diff", f"{base_sha}...{head_sha}"], repo_path)
    files = parse_diff(diff)
    if commit_msgs is None:
        commit_msgs = _run(["git", "log", "--format=%B", f"{base_sha}..{head_sha}"], repo_path)
    tests_base = _baseline_tests(repo_path)
    findings: list[dict] = []
    baselines: dict = {"tests": tests_base, "files": {}}

    def add(rule, sev, file, line, text):
        findings.append({"rule": rule, "severity": sev, "file": file, "line": line, "text": text[:200]})

    # ---- prose: commit messages + PR body ----
    template_lines = set()
    for tpl in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE/pull_request_template.md"):
        tp = repo_path / tpl
        if tp.exists():
            try:
                template_lines |= {l.strip() for l in tp.read_text(errors="ignore").splitlines() if l.strip()}
            except Exception:
                pass
    for label, text in (("commit message", commit_msgs), ("PR body", pr_body), ("PR title", pr_title)):
        for i, l in enumerate(text.splitlines(), 1):
            if l.strip() in template_lines or l.strip().replace("[x]", "[ ]") in template_lines:
                continue   # the repo's own PR template wording is never a finding
            if UNICODE_DASH.search(l):
                add("unicode-dash", "high", label, i, l)
            if EMOJI.search(l):
                add("emoji", "high", label, i, l)
            if BUZZWORDS.search(l):
                add("buzzword", "medium", label, i, l)
            if PR_FILLER.search(l):
                add("filler-phrase", "medium", label, i, l)
            if label == "commit message" and re.search(r"\*\*|^#+ ", l):
                add("markdown-in-commit", "medium", label, i, l)
    if ai_policy and re.search(r"must|required?|disclos", ai_policy, re.I) and not re.search(r"\b(ai|llm|claude|copilot|assist)", pr_body, re.I):
        add("missing-ai-disclosure", "high", "PR body", 0, "repo policy requires disclosing AI assistance; PR body does not mention it")
    # ---- code ----
    for fname, info in files.items():
        ext = Path(fname).suffix
        if ext not in CODE_EXT:
            continue
        # read the file as it exists at head_sha (not the working tree, which may be on another branch)
        full_text = _run(["git", "show", f"{head_sha}:{fname}"], repo_path)
        base = _baseline_text(full_text, ext) if full_text else {}
        baselines["files"][fname] = base
        added = info["added"]
        code_added = [(n, t) for n, t in added if t.strip()]
        comments_added = [(n, t) for n, t in code_added if _is_comment(t, ext)]
        is_test = "test" in fname.lower()
        for n, t in added:
            if UNICODE_DASH.search(t):
                add("unicode-dash", "high", fname, n, t)
            if EMOJI.search(t):
                add("emoji", "high", fname, n, t)
            if _is_comment(t, ext) or '"""' in t or "'''" in t:
                if NARRATION.match(t):
                    add("narrating-comment", "medium", fname, n, t)
                if BUZZWORDS.search(t):
                    add("buzzword", "medium", fname, n, t)
            if SEPARATOR.match(t) and not base.get("has_separators"):
                add("separator-comment", "medium", fname, n, t)
            if ext == ".py" and BROAD_EXCEPT.search(t) and not base.get("has_broad_except"):
                add("broad-except", "medium", fname, n, t)
            if ext == ".py" and re.match(r"\s*print\(", t) and not is_test:
                add("print-debug", "high", fname, n, t)
            if DOCSTRING_SECTIONS.match(t) and not base.get("has_docstring_sections"):
                add("docstring-sections-unlike-file", "low", fname, n, t)
            if re.search(r"\bpass\b\s*(#.*)?$", t) and "except" in "".join(x for _, x in added[max(0, added.index((n, t)) - 1):added.index((n, t))]):
                add("swallowed-exception", "high", fname, n, t)
        # comment density relative to file
        if len(code_added) >= 15 and not info["new_file"]:
            ratio = len(comments_added) / len(code_added)
            bl = base.get("comment_ratio", 0.05)
            if ratio > max(0.15, 2.5 * bl):
                add("comment-density", "medium", fname, code_added[0][0], f"{len(comments_added)} comment lines in {len(code_added)} added lines ({ratio:.0%}) vs file baseline {bl:.0%}")
        # docstrings on new private helpers (user rule: a short helper documents itself); near-copies of sibling docstrings
        if ext == ".py":
            existing_docs = [re.sub(r"\s+", " ", m.group(1)).strip().lower() for m in re.finditer(r'"""(.*?)"""', full_text, re.S)]
            for i, (n, t) in enumerate(added):
                if re.match(r"\s*(async\s+)?def\s+\w+\(", t):
                    j = i + 1
                    while j < len(added) and added[j][1].strip() == "":
                        j += 1
                    if j < len(added) and added[j][1].strip().startswith(('"""', "'''")):
                        is_private = re.match(r"\s*(async\s+)?def\s+_", t) is not None
                        doc_lines = []
                        for k in range(j, min(j + 12, len(added))):
                            doc_lines.append(added[k][1].strip())
                            if k > j and ('"""' in added[k][1] or "'''" in added[k][1]):
                                break
                        doc = re.sub(r"\s+", " ", " ".join(doc_lines)).strip("\"' ").lower()
                        if is_private:
                            # a file that documents its private methods makes a docstring the convention, not a tell
                            sev = "low" if base.get("private_docstring_ratio", 0) >= 0.5 else "medium"
                            add("docstring-on-private-helper", sev, fname, added[j][0], added[j][1])
                        words = set(re.findall(r"[a-z]{4,}", doc))
                        for ed in existing_docs:
                            ew = set(re.findall(r"[a-z]{4,}", ed))
                            if ed != doc and len(words) >= 8 and len(words & ew) / len(words) >= 0.75:
                                add("docstring-copies-sibling", "high", fname, added[j][0], "docstring paraphrases an existing docstring in this file")
                                break
        # type hints unlike file
        if ext == ".py" and not info["new_file"] and base.get("hint_ratio", 1) < 0.25:
            for n, t in added:
                if re.match(r"\s*def\s+\w+\(", t) and ("->" in t or re.search(r"\w+\s*:\s*\w", t.split("(", 1)[-1])):
                    add("type-hints-unlike-file", "low", fname, n, t)
        # assert messages unlike repo tests (ast-based, multi-line aware), uniform message voice, single-use helpers
        if is_test and ext == ".py" and full_text:
            added_lines = {n for n, _ in added}
            asserts = [a for a in _assert_nodes(full_text) if a[0] in added_lines]
            with_msg = [a for a in asserts if a[1]]
            const = [a for a in with_msg if a[3]]
            if len(const) >= 4 and len(const) / len(with_msg) >= 0.7 and tests_base.get("const_msg_ratio", 1) < 0.5:
                add("static-assert-messages", "medium", fname, const[0][0],
                    f"{len(const)}/{len(with_msg)} assert messages are fixed sentences with no diagnostic value; repo messages usually include the failing data")
            if len(asserts) >= 4 and len(with_msg) / len(asserts) > 0.8 and tests_base["assert_msg_ratio"] < 0.35:
                add("assert-messages-unlike-repo", "medium", fname, asserts[0][0],
                    f"{len(with_msg)}/{len(asserts)} added asserts carry messages; repo tests: {tests_base['assert_msg_ratio']:.0%}")
            modal = [a for a in with_msg if re.search(r"\b(must|should|may|shall|needs? to)\b", a[2], re.I)]
            if len(with_msg) >= 4 and len(modal) / len(with_msg) >= 0.5:
                add("uniform-assert-voice", "medium", fname, modal[0][0],
                    f"{len(modal)}/{len(with_msg)} assert messages use the same 'must/should' phrasing; drop most of them")
            sentences = [a for a in with_msg if a[3] and len(a[2].split()) >= 5]
            if len(sentences) >= 5 and len(sentences) / len(with_msg) >= 0.7:
                add("sentence-assert-messages", "low", fname, sentences[0][0],
                    f"{len(sentences)}/{len(with_msg)} assert messages are full prose sentences; keep only those a failure would not explain by itself")
            import ast
            try:
                tree = ast.parse(full_text)
                defs = [nd for nd in tree.body if isinstance(nd, ast.FunctionDef) and nd.name.startswith("_") and nd.lineno in added_lines]
                for d in defs:
                    uses = len(re.findall(r"\b%s\b" % re.escape(d.name), full_text)) - 1
                    if uses == 1:
                        add("single-use-helper", "low", fname, d.lineno, f"{d.name}() is defined for a single call site; inline it")
            except Exception:
                pass
            tests_in_file = [nd for nd in ast.parse(full_text).body if isinstance(nd, ast.FunctionDef) and nd.name.startswith("test")] if full_text else []
            for t in tests_in_file:
                n_asserts = sum(1 for nd in ast.walk(t) if isinstance(nd, ast.Assert))
                if t.lineno in added_lines and n_asserts >= 5:
                    add("assert-pile", "low", fname, t.lineno, f"{t.name} has {n_asserts} assertions; keep the ones that would fail on the base commit")
        # duplicated blocks (copy-paste of existing code instead of reuse)
        if full_text and not info["new_file"]:
            for a_line, orig_line in _duplicate_blocks(added, full_text):
                add("duplicated-block", "high", fname, a_line, f"3+ added lines duplicate existing lines starting at {orig_line}; reuse instead")
        # comment immediately above a call that just restates the call
        for i, (n, t) in enumerate(added):
            if _is_comment(t, ext) and i + 1 < len(added):
                nxt = added[i + 1][1]
                m = re.search(r"\.?(\w+)\(", nxt)
                if m:
                    words = [w for w in re.split(r"_+", m.group(1).lower()) if len(w) > 3]
                    if words and sum(1 for w in words if w in t.lower()) >= max(2, len(words) - 1):
                        add("comment-restates-call", "medium", fname, n, t)
        # every added function commented / docstring restating the name
        for i, (n, t) in enumerate(added):
            m = re.match(r"\s*def\s+(\w+)\(", t)
            if m and i + 1 < len(added):
                ds = added[i + 1][1].strip().strip('"\'').lower()
                words = [w for w in re.split(r"[_\W]+", m.group(1).lower()) if w]
                if ds and len(words) >= 2 and all(w in ds for w in words) and len(ds) < 60:
                    add("docstring-restates-name", "low", fname, n + 1, added[i + 1][1])

    weights = {"high": 12, "medium": 5, "low": 2}
    score = max(0, 100 - sum(weights[f["severity"]] for f in findings))
    counts = defaultdict(int)
    for f in findings:
        counts[f["severity"]] += 1
    return {"score": score, "findings": findings, "counts": dict(counts), "baselines": baselines,
            "verdict": "clean" if score >= 90 else "acceptable" if score >= 70 else "slop-risk"}


def format_findings(q: dict, limit: int = 30) -> str:
    if not q.get("findings"):
        return "(no AI-pattern findings)"
    return "\n".join(f"- [{f['severity']}] {f['rule']} @ {f['file']}:{f['line']}: {f['text']}" for f in q["findings"][:limit])
