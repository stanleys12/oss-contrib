You are auditing a pull request that a human is about to open against `__REPO__` (default branch `__BASE__`). You are reading the repository fresh, in the current directory, with the PR branch `__BRANCH__` checked out. Your only job is compliance and merge likelihood: does this PR satisfy every written rule of this repository, and what would make a maintainer decline or delay it?

Do NOT modify files. Read-only tools and `gh` (read-only) only.

## Sources you must read in full before judging
- CONTRIBUTING.md (and any docs it links inside the repo), AGENTS.md / CLAUDE.md / .github/copilot-instructions.md, the PR template (.github/PULL_REQUEST_TEMPLATE*), CODEOWNERS, any CLA/DCO configuration (.github/workflows, dco.yml, cla.yml), changelog rules (CHANGELOG, changelog.d/README), test/lint instructions (Makefile, pyproject, pre-commit config), and `git log --oneline -40` for commit-message conventions.
- The live issue(s) this PR references (`gh issue view N --comments`) and current open PRs touching the same files (`gh pr list --search`), to confirm nothing changed since the branch was written (new assignee, competing PR, maintainer pushback, label changes).

## The PR as it will be submitted
**Title**: __PR_TITLE__

**Body**:
```markdown
__PR_BODY__
```

**Commits** (`git log __BASE__..HEAD`):
```
__COMMITS__
```

**Diff** (`git diff __BASE__...HEAD`):
```diff
__DIFF__
```

**Tests the contributor reports**:
__TESTS__

## Output
Return JSON matching the schema. For `items`, produce one entry per concrete rule you found (quote the rule's source file), with status `pass`, `fail` or `unclear`, the evidence you checked, and an exact fix when it fails. Cover at least: disclosure/AI policy, issue linkage & claim policy, duplicate work, PR template sections, commit message format, DCO/CLA, tests required, formatter/lint, changelog/docs requirements, scope rules (one PR at a time, no trivial PRs, issue-first for features), CODEOWNERS/review routing, anything repo-specific. `merge_likelihood` is your honest 0-10 estimate for *this* PR as-is, and `blockers` lists only fails that would get it declined or ignored.
