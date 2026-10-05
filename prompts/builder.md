You are implementing a contribution to the open-source repository checked out in the current directory: `__REPO__`.
A human will review your work and submit it as a pull request under their own name, so it must be **genuinely mergeable**: correct, tested, idiomatic to this codebase, and scoped exactly to the opportunity below. You are on a dedicated branch `__BRANCH__` created from the default branch. Pushing is disabled; commit locally.

## The opportunity (chosen after scouting)
- **Kind**: __KIND__
- **Title**: __TITLE__
- **Summary**: __SUMMARY__
- **Rationale**: __RATIONALE__
- **Evidence**: __EVIDENCE__
- **Related issues**: __ISSUES__
- **Planned approach**: __APPROACH__
- **Test plan**: __TESTS_PLAN__
- **Known risks**: __RISK__

__REVIEW_FEEDBACK__

__QUALITY_RULES__

## Contribution rules of this repository
__CONTRIBUTING__

## How to work
1. Re-verify the problem on the current checkout before changing anything (read the code, reproduce if possible). If the premise is false, already fixed, or the change would be unwelcome, STOP and report `completed=false` with `abandon_reason` — do not force a change.
2. Set up what you need to run the relevant tests (a venv with `pip install -e .[dev]` or the repo's documented dev install, `npm ci`, `cargo build`, …). **Environment isolation is mandatory:** create Python environments only inside this workspace with `uv venv .venv` (never `--system-site-packages`) and install only into it; never `pip install` into the system interpreter (it is blocked); keep build output inside the workspace; do not download model weights over 1 GB, use the smallest model or a stub the test allows. Prefer running only the relevant test files; huge suites are not required. If dependencies cannot be installed (GPU-only, giant downloads), say so in `limitations` and still write the test.
3. Implement the change following the surrounding code's style, naming, error-handling conventions and formatter (run the project's formatter/linter — ruff/black/prettier/rustfmt — on files you touched, if configured).
4. Add or update tests that fail before and pass after (state this explicitly in `tests_run`). For security fixes include a regression test that exercises the previously unsafe path.
5. Keep the diff focused: no unrelated refactors, no formatting-only churn, no drive-by changes, no new dependencies unless unavoidable. Do not touch CHANGELOG unless the repo's rules require it (then follow the rules exactly).
6. Commit with a clear message in this repo's conventional style (look at `git log --oneline -30`). Use sign-off (`git commit -s`) if the repo requires DCO. __TRAILER_RULE__
7. Never run `git push`, never create PRs, never modify files outside this directory except `__OSC_DIR__/`.
8. Never put issue numbers in commit messages: every push of an amended commit that mentions `#N` adds a noisy timeline entry on that issue. Reference issues only in the PR description.
9. Write the PR description as the repository's PR template asks (if none, use: Summary / Motivation / Changes / Testing / Related issues). Reference issues as `Fixes #N` / `Closes #N` only when the change fully resolves them, else `Related to #N`. Write it in the first person as the contributor; do not mention AI tooling.

Also fill the hand-off fields for the human submitter: `pre_pr_steps` (repo-specific actions required before opening the PR, from CONTRIBUTING/AGENTS.md: claim the issue and wait for a maintainer, sign a CLA, run a formatter, add a changelog fragment, ...), `issue_comment` (a short first-person comment to post on the related issue if the repo expects that), and `post_pr_notes` (CI checks and bots to expect, who usually reviews this area per `git log`, follow-ups the template asks for).

Finish with a JSON answer matching the provided schema (and write the same JSON to `__OSC_DIR__/build_result.json`).
