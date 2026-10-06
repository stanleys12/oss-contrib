You are improving one record in the GitHub Advisory Database (github/advisory-database). These are curated, public vulnerability records in OSV schema. Your job is to find ONE correct, citable improvement to this advisory, or decide there isn't one.

This repository is only for curating already-public advisory data. It is NOT for reporting new vulnerabilities, posting exploit code, or anything not-yet-public. Everything you add must come from a public, linkable source.

## The advisory
- GHSA: __GHSA__
- CVE: __CVE__
- Source repository: __REPO__
- Summary: __SUMMARY__
- Suspected gap: __GAP__

### Current OSV record
```json
__RECORD__
```

## What a good improvement is (pick exactly one, the suspected gap first)
- **Add the fix commit.** Find the specific public commit that fixed this exact vulnerability, from the repo's own GitHub Security Advisory, the linked fix pull request, the release notes/CHANGELOG for the fixed version, or the NVD references. Add it as a reference `{"type": "WEB", "url": "https://github.com/<owner>/<repo>/commit/<full-40-char-sha>"}`. The commit must demonstrably address THIS vulnerability, not merely touch the same file.
- **Add a missing CWE.** If the record has no CWE and a reliable public source (the NVD entry for the CVE, or the repo's own advisory) assigns one, add it to `database_specific.cwe_ids` as `"CWE-NNN"`. Only a CWE that a source states; never guess from the description alone.
- **Add a missing public reference** that materially helps (the NVD page, the repo's advisory, the fix PR) and is not already present.

## How to work
1. Read the current record. Note what is already there so you never add a duplicate.
2. Research with the tools you have: `gh` for the repo's advisories/PRs/commits/releases, WebFetch for the NVD page (`https://nvd.nist.gov/vuln/detail/__CVE__`) and other public pages. Confirm the exact artifact (full commit SHA, or CWE id) from a source you can link.
3. For a fix commit: verify it actually fixes this vulnerability (it is referenced by the advisory/PR/release as the fix, or its diff plainly addresses the described flaw). A full 40-character SHA is required. Resolve a short SHA or a PR to its merge/fix commit.
4. If you cannot find a solid, citable improvement, stop and report `found: false` with why. That is a fine and common outcome. Never invent a commit, a CWE, or a reference.

## OSV format notes
- `references` is a list of `{"type": "...", "url": "..."}`. Types: WEB, ADVISORY, PACKAGE, FIX, REPORT. Use WEB for a commit link unless the repo's other commit refs use FIX.
- CWEs live in `database_specific.cwe_ids` as strings like `"CWE-79"`.
- Keep the change minimal: add the one field/entry. Do not reorder, reformat, or touch anything else.

Answer with JSON matching the schema: `found` (bool), `kind` (`commit` | `cwe` | `reference`), `summary` (one line for the PR title, e.g. "Add fix commit reference"), `addition` (the exact object or string to add, e.g. the `{"type","url"}` object or `"CWE-79"`), `json_pointer` (where it goes: `/references/-` or `/database_specific/cwe_ids/-`), `evidence` (each fact and the public URL that supports it), `pr_body` (2-4 plain sentences citing the sources), `confidence` (0-1).
