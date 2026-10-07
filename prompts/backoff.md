A maintainer of `__REPO__` reacted to a pull request we opened. Decide whether this means the project does not want our contributions as a rule (so we should stop sending PRs there entirely), or whether it is about this one PR (so we keep the repo and just learn from it).

We open PRs without being invited: the system finds an open issue, builds a fix, and opens a PR. The PRs are AI-assisted and disclose that. So a project that rejects unsolicited or AI-assisted contributions *as a policy* is one we should stop contributing to.

## What the maintainer said
PR: __PR_URL__  (__PR_TITLE__)
State: __STATE__

Maintainer messages (and any issue/PR they referenced):
__CONTEXT__

## Decide
Return `stop: true` only when the message shows the project does not want contributions of our kind going forward. Clear cases:
- "we don't accept AI / automated / bot-generated PRs", "no unsolicited PRs", "please stop opening PRs", "closing as spam", "AI slop".
- A stated policy that outside or uninvited contributors should not open PRs at all.

Return `stop: false` when the reason is specific to this PR or is normal review, even if the word "unsolicited" or "spam" appears. Clear cases:
- The change is trivial/duplicate/already being handled by someone else, but the maintainer welcomes other or more substantial contributions ("please submit more substantial PRs in the future" means they DO want good PRs).
- A technical disagreement ("wrong approach", "not needed", "we'll do it differently").
- Closed in favor of another PR or an internal fix.
- The maintainer is asking a question or giving feedback, not telling us to stop.

When genuinely unsure, return `stop: false` and let a human decide — a wrong permanent ban costs more than one more PR. Read what they actually mean, not just the words they used.

Answer with JSON: {"stop": true/false, "reason": "one sentence on what the maintainer actually objected to"}.
