You are deciding how a security fix the pipeline built for `__REPO__` should be delivered, and, if it is a genuine not-yet-public vulnerability, drafting a private report to the maintainers.

A security fix can be delivered two ways:
- **Public pull request** — correct when the flaw is ALREADY public: there is a CVE, a published advisory, or an open issue that describes it. Nothing is lost by a public patch.
- **Private vulnerability report** — correct when the flaw is NOT yet public and a public patch would hand attackers an exploit before users can update. This is responsible disclosure.

## The built change
- Title: __TITLE__
- What it fixes (our records): __SUMMARY__
- Why / impact: __WHY__
- Related issues the scout found: __ISSUES__

```diff
__DIFF__
```

## How to decide
1. Search for public disclosure of THIS specific flaw: `gh` for the repo's open/closed issues, its published security advisories (`gh api repos/__REPO__/security-advisories`), and its CVEs; WebFetch the NVD if a CVE is suspected. A general open issue that happens to touch the same file is not disclosure of this vulnerability.
2. Judge whether this is actually a security vulnerability (a flaw an attacker could exploit for a real impact: RCE, auth bypass, data exposure, injection, SSRF, path traversal, DoS), not just a correctness bug or hardening with no attacker.
3. Decide:
   - `public_pr`: it is a security issue that is already public, OR it is not really a security vulnerability. It goes through the normal public PR flow.
   - `private`: it is a real, exploitable vulnerability with no public disclosure yet. Draft the private report.
   - `unsure`: you cannot tell if it is already public, or cannot judge the impact. Leave it for the human.

## If private, draft the report (this goes to the maintainers through GitHub private vulnerability reporting)
- `report_summary`: one or two sentences, what the vulnerability is.
- `report_details`: the vulnerability, the affected code path (file and function), the conditions to trigger it, and the impact. Describe the mechanism plainly so a maintainer can confirm it. **Do NOT include a working exploit, payload, or step-by-step attack recipe.** Enough to understand and verify, not to weaponize.
- `affected_versions`: which versions are affected, as far as you can tell (e.g. ">= 0 (all released)" or "since <version>").
- `cwe`: the most fitting CWE id, or "" if unsure.
- `severity`: low / medium / high / critical, with one line of reasoning.
- `fix_note`: that a fix is ready and can be shared or opened as a PR once they confirm, and that this was found with AI assistance.

The report must contain nothing about this machine, no tokens, no local paths. It is addressed to maintainers who will coordinate the fix.

Answer with JSON matching the schema.
