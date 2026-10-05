## Flagship polish pass: make this PR the one a maintainer merges on sight

This branch already contains a working change. Your job now is to maximise the chance that the maintainers of `__REPO__` approve it quickly. Take as long as you need. Work in this order and write down what you learn as you go (in your final `summary`), because the human submitter will use it to answer reviewers.

### 1. Learn how this repo actually merges outside contributions
- `gh pr list --repo __REPO__ --state merged --limit 60 --json number,title,author,mergedAt,additions,deletions,files` and read the 5-8 merged PRs closest to this one (same files/area, similar size). Note: title format, body sections, how tests are written and named, typical diff size, whether changelog/docs edits are expected, whether the PR references an issue, how AI assistance is disclosed (if at all).
- Read maintainers' review comments on those PRs (`gh pr view N --comments`, `gh api repos/__REPO__/pulls/N/comments`): what do they ask contributors to change? Naming, tests, scope, style? Do the same for 3-5 *closed-unmerged* outside PRs in this area: why were they rejected?
- Identify the likely reviewer(s) for the touched files (`git log --format='%an' -- <file> | sort | uniq -c | sort -rn | head`, CODEOWNERS) and any of their stated preferences.

### 2. Re-examine the design
- List the alternative ways this could have been fixed. For each: what would the maintainers prefer, judging by the codebase and their comments? If a different approach is clearly better aligned with the repo, switch to it now. If the current approach is right, be able to say why in one paragraph (that goes into the PR body only if a reviewer would otherwise ask).
- Check every call site / consumer of what you changed; check behaviour on the edge cases the code's comments, tests and issues mention.
- Search open and closed issues for the same symptom; make sure the PR references the canonical one and mentions any prior attempt.

### 3. Prove it
- Run the repo's real CI commands locally for the touched crate/package (read .github/workflows to find the exact commands: formatters, linters, type checkers, test invocations). Every one must pass. Fix anything they flag.
- The tests must fail without the change and pass with it; state the exact commands and results.
- Add the one extra test a skeptical maintainer would ask for (an edge case, a regression guard), and remove any test that only pads.

### 4. Make it read like the repo's own
- Diff scope: only what the fix needs. No incidental cleanups, no reformatting of untouched lines, no new dependencies.
- Naming, comments, docstrings, error messages: mirror the surrounding code and the merged PRs you read. Delete comments that narrate the code.
- Commit message and PR title in the exact style of the merged PRs. PR body in the exact section structure the repo uses (or its template), short, factual, with a before/after or reproduction where the repo's PRs do that. Keep the AI-assistance disclosure that the repo's policy requires, phrased plainly; if the repo's CI or policy rejects AI co-author trailers, make sure none is present.

### 5. Adversarial pass
- Re-read the final diff as the strictest reviewer of this repo. For every line ask: would they accept this as-is? Fix or justify.

Finish with the usual JSON. In `summary`, include: the conventions you found (bullet list), the alternatives you considered and why this one, the exact verification commands and results, and anything the human should be ready to answer.
