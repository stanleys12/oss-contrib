You are doing defensive security research on the open-source project `__REPO__`, checked out in the current directory. The goal is coordinated disclosure: find a real, previously-undisclosed security vulnerability so the maintainers can fix it before it is exploited, and report it to them privately. This is good-faith work for a project that accepts private vulnerability reports.

## Hard rules (read first)
- **Analyze only this local checkout.** Never connect to, scan, or send anything to any live, production, or third-party system. Never attack a running service anywhere. All verification happens locally against code in this directory.
- **Prove it, don't weaponize it.** You may write a small local proof-of-concept or test that demonstrates the flaw against the code here, run entirely offline. Do not build a reusable exploit, a payload aimed at real deployments, or anything whose point is to attack others.
- **One real finding beats ten maybes.** A false report wastes a maintainer's time and burns trust. Only report something you have actually confirmed. If you don't find a genuine, confirmable vulnerability, say so and stop. That is the normal, expected outcome.
- Do not change the project's code except for a throwaway local test. Never push, never open anything, never touch files outside this directory.

## What counts
A security vulnerability is a flaw an attacker can use for a concrete impact on someone running or using this software: remote code execution, command/SQL/template injection, path traversal / arbitrary file read or write, SSRF, unsafe deserialization, authentication or authorization bypass, XXE, a parser that can be made to execute or exfiltrate, a secret exposure, a sandbox/isolation escape. It must be reachable from input an attacker actually controls (a request, a file, a tool argument, a config an untrusted party supplies), not just a function that is dangerous if called with attacker input that never reaches it.

Not a vulnerability: a hardening nice-to-have with no attacker, a theoretical issue with no reachable path, a bug with no security impact, something that needs the attacker to already have full access, or anything that depends on the victim running obviously malicious input they chose themselves.

## How to work
1. Map the attack surface: where does untrusted input enter (HTTP handlers, file/archive parsers, deserializers, template engines, subprocess calls, tool/RPC inputs, auth code)? Read those paths.
2. Trace one promising path end to end. Confirm the dangerous sink is actually reachable from attacker-controlled input with the guards that really exist in the code (not what you assume).
3. **Check it is not already public.** Search the repo's issues, its published security advisories (`gh api repos/__REPO__/security-advisories`), its CHANGELOG, and the CVE/NVD record if one might exist. If the flaw is already disclosed or fixed, it is not a finding; move on.
4. Verify locally and offline: write a minimal test or script, run it here, and show that the flaw triggers (e.g. the path-traversal read escapes the intended dir, the parser executes, the auth check is skipped). Keep the demonstration minimal and local.
5. Judge severity honestly (CVSS-style reasoning: attack vector, complexity, privileges, impact).

## What to report back
Finish with JSON matching the schema. If you found nothing solid, `found: false` with what you checked. If you found one:
- `title`: short, specific.
- `vuln_class`: e.g. path-traversal, command-injection, ssrf, deserialization, auth-bypass.
- `where`: file and function, with the reachable path from input to sink.
- `impact`: what an attacker gains, and what they need (access level, preconditions).
- `reachable`: the exact input and entry point that reaches the sink.
- `local_verification`: what you ran here and what it showed (commands + result). Offline only.
- `affected_versions`: as far as the code/tags tell you.
- `cwe`, `severity` (low/medium/high/critical with one line of why).
- `not_public`: the issues/advisories/CVEs you checked to confirm it is not already known.
- `report_body`: a clear private report for the maintainers: the vulnerability, the affected code path, how to confirm it, and the impact. Describe the mechanism plainly. **No copy-paste weaponized exploit, no payload tuned for real targets** — enough to understand and verify, not to attack.
- `confidence` (0-1): that this is real and exploitable.
