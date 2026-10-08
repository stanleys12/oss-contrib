# osc — Open Source Contribution Engine

An isolated, end-to-end system for finding well-known open-source repositories worth contributing
to (LLM inference / post-training / security / agents / core ML / frontier), analyzing each one in
depth, building a **meaningful, reviewed** change on a local branch, and opening the PR.

It started as a hand-off tool (you click to open each PR). Since October 2026 it runs autonomously
on my account: launchd jobs scout, build and open PRs that pass every gate, answer review feedback
and push follow-up fixes, and answer GitHub Discussions questions. All of that work is done by Claude
Code agents. PRs disclose AI assistance the way each repo asks, commits keep the AI co-author trailer
where the repo allows it, and anything that needs a person (CLAs, attestations, identity questions,
projects that want human-written text) is left for me.

```
scan ──► rank ──► analyze (clone + issue triage + static analysis + scout agent)
     ──► opportunities ──► build (builder agent) ──► review (independent reviewer agent)
     ──► ready change (diff + why + tests + PR body) ──► YOU: prepare (fork+push) → gh pr create
```

## Autonomous mode

| Job | When | What |
|---|---|---|
| `osc.daily` | 02:00, 08:00, 14:00, 20:00 | housekeeping, PR states, scout, build, review, compliance, open, email digest |
| `osc.quota` | hourly | keeps going until the daily PR target is met |
| `osc.responder` | every 30 min | answers maintainer feedback, fixes conflicts and red CI, pushes, replies |
| `osc.discuss` | 10:15 | answers up to 2 unanswered Discussions Q&A questions, checked against the code |
| `osc.housekeeping --guard` | every 30 min | keeps disk use bounded |

Gates before anything goes public: reviewer approval, an AI-pattern lint, a compliance agent that
reads the repo's own rules, duplicate and claim checks against open PRs and issues, and a second
agent that checks every factual sentence in replies.

## Quick start
```bash
cd ~/oss-contrib
.venv/bin/python osc.py serve            # dashboard on http://127.0.0.1:8791 (+ background worker)
.venv/bin/python osc.py scan             # discover + score ~1500 candidate repos (≈40 min, cached 6h)
.venv/bin/python osc.py rank --n 30      # ranked table with score components
.venv/bin/python osc.py analyze NVIDIA/garak      # deep analysis → 3–6 opportunities (scout agent, ~$1–3)
.venv/bin/python osc.py opps             # list opportunities with priority
.venv/bin/python osc.py build <opp-id>   # implement + review on branch osc/<slug> (builder=opus, ~$5–20)
.venv/bin/python osc.py changes
.venv/bin/python osc.py prepare <change-id>   # fork + push branch to YOUR fork; prints the gh pr create command
```
All of the above can be driven from the UI (Overview → actions; Repo page → Analyze; Opportunity → Build;
Change → PR & submit → Prepare).

## Stages
1. **Scan** (`osc/scanner.py`) — sources: 390 curated seeds in `osc/config.py` + GitHub topic/keyword
   search per domain. One GraphQL profile per repo → 7 transparent score components:
   domain fit, popularity, activity, **external-contributor PR merge ratio** (last 60 closed PRs by
   non-members), approachability (good-first-issue/help-wanted counts, CONTRIBUTING), language fit,
   PR crowding. CLA/DCO requirements are detected from CONTRIBUTING.
2. **Analyze** (`osc/analyzer.py`) — shallow clone into `workspace/`, fetch open issues and triage for
   *claimability* (unassigned, no linked PR, no negative labels), index open PR titles for duplicate
   checks, collect static signals (ruff bug rules, bandit, pip-audit, TODO/FIXME, churn), then run the
   **scout** agent (headless Claude Code, read-only tools) with `prompts/scout.md`. Output: ranked
   opportunities with evidence, approach, test plan, risk, confidence, acceptance likelihood.
3. **Build** (`osc/contributor.py`) — branch `osc/<slug>-xxxx` from the default branch; the **builder**
   agent (`prompts/builder.md`) re-verifies the premise, implements, adds tests, runs them, commits in the
   repo's style (DCO sign-off if required). Then the **reviewer** agent (`prompts/reviewer.md`, separate
   context, read-only) grades meaningfulness / correctness / style / tests / merge likelihood and either
   approves, requests fixes (→ builder round 2), or rejects. Push to `origin` is disabled in the clone.
   Before every review, `osc/quality.py` runs a deterministic **AI-pattern lint** on the diff, commit messages and PR
   text: unicode dashes, emoji, narrating comments, buzzwords, comment density vs. the file, docstring / type-hint /
   assert-message habits vs. the repo, duplicated blocks instead of reuse, broad excepts, print debugging, filler
   phrases, static assert messages with no diagnostic value, uniform "must/should" assert voice, single-use test
   helpers, assertion piles, comments that restate the call below them. Findings go to the reviewer (which also runs its own machine-generated-code audit and scores
   `reads_human`); a change cannot become `ready` below `MIN_QUALITY_SCORE` (default 85). The builder prompt carries
   `prompts/code_quality_rules.md` ("write like a long-time maintainer, not like a model").
4. **Hand-off** — the Change page shows Why / Diff / Tests / Review / PR body. *Prepare* forks the repo to
   your account and pushes the branch; it prints the exact `gh pr create` command and the compare URL.
   You open the PR.

## Policy gates (added 09-25)
The analyzer reads AGENTS.md / CLAUDE.md / CONTRIBUTING / PR template and records three things per repo:
`ai_policy` (disclosure rules → surfaced on the hand-off checklist), `ai_prohibited` (repo forbids AI-written PRs or
won't review them from first-timers → repo marked *skipped*, builds refuse) and `ai_prose_human` (PR text / commit
messages must be human-written → Playbook step tells you to rewrite them). transformers and trl are skipped on this basis.

## Revision loop & Playbook
`revise` (job kind / Change page button) re-runs the builder with the reviewer's required fixes + lint findings, then
lint + review again; `rereview` runs lint + reviewer only. `#/playbook` lists every ready change with the exact steps:
policy stops, repo pre-PR steps, the issue comment to post, the diff to read, Prepare, the pre-filled PR link, mark submitted.
`scripts/batch_analyze.py -j 3 a/b c/d …` scouts several repos concurrently; `scripts/revise.py` / `scripts/rereview.py` run
rounds outside the worker.

## Layout
```
osc.py            CLI
osc/config.py     domains, seeds, weights, tunables (settings.json / OSC_* env overrides)
osc/scanner.py    discovery + scoring        osc/analyzer.py   clone, triage, signals, scout
osc/contributor.py builder/reviewer loop, prepare    osc/claude_runner.py  headless Claude w/ live event mirroring
osc/pipeline.py   job queue + worker         osc/server.py     FastAPI API + SSE
ui/               dashboard (vanilla JS)     prompts/          scout / builder / reviewer prompts
data/             sqlite db, reports/<repo>/… (issues, signals, opportunities, change diffs, transcripts)
workspace/        clones (one per repo; branches live here)      logs/  run logs
```

## Notes
- GitHub token: taken from `OSC_GITHUB_TOKEN` / `GH_TOKEN`, else the git credential helper (keychain).
- Claude binary: `OSC_CLAUDE_BIN`, else `claude` on PATH, else the newest VS Code extension binary.
- Costs are tracked per change (`cost_usd`) and shown in the UI. Budget caps: scout $6, builder $25/round, reviewer $6.
- **AI disclosure**: `AI_COAUTHOR_TRAILER` (Settings, default on) keeps Claude Code's `Co-authored-by: Claude` trailer on
  commits. The analyzer reads AGENTS.md / CLAUDE.md / CONTRIBUTING and surfaces any AI-contribution policy on the
  change's hand-off checklist (garak, for example, *requires* the trailer and a disclosure section in the PR body).
  You are the submitter: review every diff before opening a PR. After *Prepare*, the PR tab shows a single
  "Open pre-filled PR form" link (GitHub compare URL with title + body pre-filled); you press "Create pull request".
- `scripts/start.sh` / `scripts/stop.sh` start/stop the dashboard + worker.

## First live run (2026-09-24)
- Scan: 1,530 repos scored in 35 min (1,641 API calls).
- Analyze NVIDIA/garak: scout 69 turns, $2.33 → 3 opportunities (discarded ~12 leads already claimed/duplicated).
- Build top opportunity (LRLBuff append-vs-replace bug, issue #791): builder (opus) + reviewer (sonnet), 2 rounds,
  approved 8/10, $3.76, 134-line diff incl. a new test module. Independently verified: new tests pass on the branch,
  3/4 fail on main, existing `tests/test_attempt.py` still passes. Branch: `osc/lrlbuff-untransform-corrupts-conversatio-6281`.
