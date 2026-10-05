"""Headless Claude Code runner.

Runs `claude -p` with stream-json output so every assistant step / tool call is mirrored into
the events table (the UI tails it). Returns the final result text, structured output (if a
JSON schema was given), cost and turn count.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, log


@dataclass
class ClaudeResult:
    ok: bool
    text: str = ""
    structured: dict | list | None = None
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str = ""
    duration_s: float = 0.0
    error: str = ""
    transcript_path: str = ""
    tool_calls: list = field(default_factory=list)


def _summarize_tool(name: str, inp: dict) -> str:
    try:
        if name in ("Read", "Write", "Edit", "MultiEdit"):
            return f"{name} {inp.get('file_path', '')}"
        if name == "Bash":
            return f"Bash: {str(inp.get('command', ''))[:160]}"
        if name in ("Grep", "Glob"):
            return f"{name} {inp.get('pattern', '')} {inp.get('path', '') or ''}"
        if name in ("WebFetch", "WebSearch"):
            return f"{name} {inp.get('url') or inp.get('query', '')}"
        if name == "Agent":
            return f"Agent: {str(inp.get('description', ''))[:100]}"
        return f"{name} {json.dumps(inp)[:120]}"
    except Exception:
        return name


def run_claude(prompt: str, cwd: Path, *, model: str, max_turns: int, stage: str, repo: str | None,
               allowed_tools: list[str] | None = None, disallowed_tools: list[str] | None = None,
               skip_permissions: bool = False, json_schema: dict | None = None, max_budget_usd: float | None = None,
               append_system_prompt: str | None = None, transcript_path: Path | None = None, settings_json: dict | None = None,
               env_extra: dict | None = None, timeout_s: int = 3600) -> ClaudeResult:
    bin_ = config.claude_binary()
    cmd = [bin_, "-p", "--verbose", "--output-format", "stream-json", "--model", model, "--max-turns", str(max_turns)]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    if disallowed_tools:
        cmd += ["--disallowedTools", *disallowed_tools]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if max_budget_usd:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if settings_json:
        cmd += ["--settings", json.dumps(settings_json)]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)              # allow nesting when launched from inside Claude Code
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env["GH_TOKEN"] = config.github_token()  # lets the agent use `gh` read-only for issue/PR research
    env["GIT_TERMINAL_PROMPT"] = "0"
    if env_extra:
        env.update(env_extra)

    t0 = time.time()
    res = ClaudeResult(ok=False)
    lock = cwd / ".osc_agent_lock"
    try:
        lock.write_text(f"{stage} {os.getpid()} {int(t0)}\n")
    except Exception:
        lock = None
    tp = transcript_path or (config.LOGS_DIR / f"claude-{stage}-{int(t0)}.jsonl")
    res.transcript_path = str(tp)
    log.info(stage, f"claude start model={model} max_turns={max_turns} cwd={cwd.name}", repo=repo,
             data={"cmd": " ".join(c if len(c) < 60 else c[:57] + "..." for c in cmd[1:])})
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
    except FileNotFoundError as e:
        res.error = f"claude binary missing: {e}"
        return res
    assert proc.stdin and proc.stdout
    proc.stdin.write(prompt)
    proc.stdin.close()
    last_text = ""
    with open(tp, "w") as tf:
        for line in proc.stdout:
            tf.write(line)
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("type")
            if et == "system" and ev.get("subtype") == "init":
                res.session_id = ev.get("session_id", "")
            elif et == "assistant":
                for blk in (ev.get("message") or {}).get("content", []):
                    if blk.get("type") == "text" and blk.get("text", "").strip():
                        last_text = blk["text"]
                        log.info(stage, "💬 " + last_text.strip()[:400], repo=repo)
                    elif blk.get("type") == "tool_use":
                        s = _summarize_tool(blk.get("name", "?"), blk.get("input") or {})
                        res.tool_calls.append(s)
                        log.info(stage, "🔧 " + s, repo=repo)
            elif et == "result":
                res.text = ev.get("result") or last_text
                res.cost_usd = float(ev.get("total_cost_usd") or 0)
                res.turns = int(ev.get("num_turns") or 0)
                res.structured = ev.get("structured_output")
                res.ok = not ev.get("is_error", False)
                if ev.get("is_error"):
                    res.error = str(ev.get("result") or ev.get("error") or "unknown error")[:2000]
            if time.time() - t0 > timeout_s:
                proc.kill()
                res.error = f"timeout after {timeout_s}s"
                break
    stderr = proc.stderr.read() if proc.stderr else ""
    proc.wait()
    if lock:
        try:
            lock.unlink()
        except Exception:
            pass
    res.duration_s = round(time.time() - t0, 1)
    if proc.returncode != 0 and not res.error:
        res.error = f"exit {proc.returncode}: {stderr[-1500:]}"
        res.ok = False
    lvl = log.ok if res.ok else log.error
    lvl(stage, f"claude done ok={res.ok} turns={res.turns} cost=${res.cost_usd:.2f} {res.duration_s}s"
        + (f" err={res.error[:200]}" if res.error else ""), repo=repo)
    return res


def extract_json(text: str):
    """Best-effort: find the largest JSON object/array in free text."""
    if not text:
        return None
    text = text.strip()
    for cand in (text,):
        try:
            return json.loads(cand)
        except Exception:
            pass
    import re
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    return None
