OPPORTUNITIES_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_notes": {"type": "string", "description": "Contribution rules observed (CLA/DCO, tests, formatting, issue-first policy) and overall assessment."},
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["bug", "security", "feature", "robustness", "performance", "tests", "docs-substantive", "other"]},
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "2-4 sentences: what is wrong / missing and what the change does."},
                    "rationale": {"type": "string", "description": "Why maintainers should want this; user impact; link to maintainer signals."},
                    "evidence": {"type": "array", "items": {"type": "string"}, "description": "file:line references, commands run, reproduction output."},
                    "related_issues": {"type": "array", "items": {"type": "integer"}},
                    "related_prs": {"type": "array", "items": {"type": "integer"}, "description": "Open/merged PRs that touch the same area (for duplicate transparency)."},
                    "approach": {"type": "string", "description": "Concrete implementation plan: files, functions, tests to add."},
                    "tests_plan": {"type": "string"},
                    "scope": {"type": "string", "description": "Estimated files and lines changed."},
                    "risk": {"type": "string", "description": "What could break; backwards-compat concerns."},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "That the problem is real and the plan works."},
                    "accept_likelihood": {"type": "number", "minimum": 0, "maximum": 1, "description": "That maintainers merge a clean PR for this."},
                    "duplicate_check": {"type": "string", "description": "What you searched and found."},
                },
                "required": ["kind", "title", "summary", "rationale", "evidence", "approach", "scope", "risk", "confidence", "accept_likelihood", "duplicate_check"],
            },
        },
    },
    "required": ["repo_notes", "opportunities"],
}

BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "completed": {"type": "boolean"},
        "summary": {"type": "string", "description": "What was changed and why, in plain language (3-8 sentences)."},
        "why": {"type": "string", "description": "The justification a maintainer needs: the bug/gap, its impact, evidence."},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "object", "properties": {"command": {"type": "string"}, "result": {"type": "string"}}, "required": ["command", "result"]}},
        "limitations": {"type": "string", "description": "Anything not verified, environment limits, follow-ups."},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string", "description": "Full PR description in Markdown following the repo's PR template if one exists."},
        "abandon_reason": {"type": "string", "description": "If completed=false: why this opportunity should be dropped."},
        "pre_pr_steps": {"type": "array", "items": {"type": "string"}, "description": "Repo-specific things the human must do BEFORE opening the PR, taken from CONTRIBUTING/AGENTS.md: e.g. comment on the issue and wait, sign the CLA, open a discussion, run a formatter. Empty if none."},
        "issue_comment": {"type": "string", "description": "If the repo expects contributors to claim/discuss the issue first: a short, plain comment (2-5 sentences, first person, no AI mention unless the policy requires it) the human can post on the related issue. Empty string otherwise."},
        "post_pr_notes": {"type": "array", "items": {"type": "string"}, "description": "What to expect after opening: required labels, CI names to watch, typical reviewer, DCO/CLA bots, anything the PR template asks the author to do afterwards."},
    },
    "required": ["completed", "summary", "why", "files_changed", "tests_run", "limitations", "pr_title", "pr_body", "pre_pr_steps", "issue_comment", "post_pr_notes"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        "score": {"type": "number", "minimum": 0, "maximum": 10, "description": "Overall merge-readiness."},
        "meaningfulness": {"type": "number", "minimum": 0, "maximum": 10},
        "correctness": {"type": "number", "minimum": 0, "maximum": 10},
        "style_conformance": {"type": "number", "minimum": 0, "maximum": 10},
        "test_quality": {"type": "number", "minimum": 0, "maximum": 10},
        "merge_likelihood": {"type": "number", "minimum": 0, "maximum": 10},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "required_fixes": {"type": "array", "items": {"type": "string"}, "description": "Blocking problems the builder must fix before this is ready."},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "maintainer_perspective": {"type": "string", "description": "How a maintainer of this repo would likely react and why."},
        "reads_human": {"type": "number", "minimum": 0, "maximum": 10, "description": "10 = indistinguishable from a careful long-time contributor; 0 = obviously machine-generated."},
        "slop_findings": {"type": "array", "items": {"type": "string"}, "description": "Concrete file:line instances of machine-generated-code patterns (see audit list). Each one is a required fix."},
    },
    "required": ["verdict", "score", "meaningfulness", "correctness", "style_conformance", "test_quality", "merge_likelihood", "strengths", "required_fixes", "suggestions", "maintainer_perspective", "reads_human", "slop_findings"],
}

COMPLIANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "rule": {"type": "string"}, "source": {"type": "string"},
            "status": {"type": "string", "enum": ["pass", "fail", "unclear"]},
            "evidence": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["rule", "source", "status", "evidence", "fix"]}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "merge_likelihood": {"type": "number", "minimum": 0, "maximum": 10},
        "live_changes": {"type": "string", "description": "Anything that changed on GitHub since the branch was written (new PRs, comments, assignees, labels) and its impact."},
        "summary": {"type": "string"},
    },
    "required": ["items", "blockers", "merge_likelihood", "live_changes", "summary"],
}
