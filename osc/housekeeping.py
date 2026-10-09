"""Disk hygiene for the pipeline.

Everything the pipeline downloads or builds is kept under the project root so it can be
reclaimed without touching anything else on the machine:

* caches (Hugging Face hub, uv, pip, npm) are redirected into ``<project>/.cache`` via
  :func:`isolated_env` / :func:`apply_isolation`, and ``PIP_REQUIRE_VIRTUALENV`` blocks
  installs into the system interpreter;
* :func:`cleanup` removes clones and build artifacts that no pending work needs, then
  trims the oldest clones until the workspace is under its size cap.

Every deletion is checked to be inside the project root first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, db, log

STAGE = "housekeeping"
PROJECT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT / "workspace"
CACHE = PROJECT / ".cache"
LOCK = ".osc_agent_lock"

# directories inside a clone that are pure build/dependency output and can always be rebuilt
HEAVY_DIRS = (".venv", "venv", "node_modules", "target", "build", "_build", "_build_static",
              "build-dir", "cmake-build-debug", "cmake-build-release", ".tox", ".pytest_cache",
              "__pycache__", ".mypy_cache", ".ruff_cache", "dist", ".local")
# statuses that mean the workspace may still be needed for a revision
LIVE_STATUSES = ("ready", "prepared", "approved", "needs_work")


def isolated_env() -> dict[str, str]:
    c = CACHE
    for sub in ("hf", "uv", "pip", "npm", "xdg", "go", "gomod", "pnpm", "yarn", "torch", "gradle", "corepack"):
        (c / sub).mkdir(parents=True, exist_ok=True)
    return {
        "HF_HOME": str(c / "hf"),
        "HF_HUB_CACHE": str(c / "hf" / "hub"),
        "TRANSFORMERS_CACHE": str(c / "hf" / "hub"),
        "UV_CACHE_DIR": str(c / "uv"),
        "PIP_CACHE_DIR": str(c / "pip"),
        "XDG_CACHE_HOME": str(c / "xdg"),
        "npm_config_cache": str(c / "npm"),
        "npm_config_store_dir": str(c / "pnpm"),      # pnpm store (default is ~/Library/pnpm)
        "PNPM_STORE_DIR": str(c / "pnpm"),
        "YARN_CACHE_FOLDER": str(c / "yarn"),
        "COREPACK_HOME": str(c / "corepack"),
        "GOCACHE": str(c / "go"),
        "GOMODCACHE": str(c / "gomod"),
        "GOFLAGS": "-modcacherw",                     # module cache files deletable without chmod
        "TORCH_HOME": str(c / "torch"),
        "GRADLE_USER_HOME": str(c / "gradle"),
        "PIP_REQUIRE_VIRTUALENV": "1",      # pip refuses to install outside a venv
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }


def apply_isolation() -> None:
    """Export the isolated cache locations into this process so every child inherits them."""
    os.environ.update(isolated_env())


def _inside_project(p: Path) -> bool:
    try:
        p.resolve().relative_to(PROJECT.resolve())
        return True
    except ValueError:
        return False


_DU_CACHE: dict[str, int] = {}


def _du_mb(p: Path, cached: bool = True) -> int:
    key = str(p)
    if cached and key in _DU_CACHE:
        return _DU_CACHE[key]
    r = subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True)
    try:
        mb = int(r.stdout.split()[0]) // 1024
    except (ValueError, IndexError):
        mb = 0
    _DU_CACHE[key] = mb
    return mb


def _free_gb(path: Path = PROJECT) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def _rm(p: Path, dry: bool, why: str, freed: list[int]) -> None:
    if not _inside_project(p) or not p.exists():
        return
    mb = _du_mb(p)
    log.info(STAGE, f"{'would remove' if dry else 'removing'} {p.relative_to(PROJECT)} ({mb} MB): {why}")
    if not dry:
        shutil.rmtree(p, ignore_errors=True)
        for k in [k for k in _DU_CACHE if k == str(p) or str(p).startswith(k + "/") or k.startswith(str(p) + "/")]:
            _DU_CACHE.pop(k, None)
    freed.append(mb)


def _newest_mtime(p: Path) -> float:
    try:
        return max(p.stat().st_mtime, max((c.stat().st_mtime for c in p.iterdir()), default=0))
    except OSError:
        return 0


def _clone_state(full_name: str) -> dict:
    rows = db.rows("SELECT status, pr_url, updated_at FROM changes WHERE repo=?", (full_name,))
    now = time.time()
    return {
        "open_pr": any(r["status"] == "submitted" and r["pr_url"] for r in rows),
        "live": any(r["status"] in LIVE_STATUSES and now - r["updated_at"] < 14 * 86400 for r in rows),
        "stale_needs_work": bool(rows) and all(
            r["status"] in ("needs_work", "rejected", "abandoned", "failed", "merged", "closed")
            for r in rows) and all(now - r["updated_at"] > 7 * 86400 for r in rows if r["status"] == "needs_work"),
        "nothing": not rows,
        "rows": len(rows),
    }


def cleanup(dry_run: bool = False, max_workspace_gb: float | None = None,
            min_free_gb: float | None = None, heavy_age_days: float = 2.0) -> dict:
    """Reclaim space. Returns a summary. Safe to run while agents work: locked clones are skipped."""
    max_ws = max_workspace_gb or float(config.setting("MAX_WORKSPACE_GB"))
    min_free = min_free_gb or float(config.setting("MIN_FREE_GB"))
    freed: list[int] = []
    kept: list[tuple[float, Path, str]] = []
    now = time.time()
    WORKSPACE.mkdir(exist_ok=True)
    _DU_CACHE.clear()

    for d in sorted(WORKSPACE.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if (d / LOCK).exists():
            kept.append((now, d, "locked"))
            continue
        full = d.name.replace("__", "/", 1)
        st = _clone_state(full)
        repo = db.row("SELECT status FROM repos WHERE full_name=?", (full,)) or {}
        if st["nothing"] and repo.get("status") in ("skipped", "error", None):
            _rm(d, dry_run, "no change was ever built here and the repo is skipped/unknown", freed)
            continue
        if st["nothing"] and _newest_mtime(d) < now - 3 * 86400:
            _rm(d, dry_run, "analysed but nothing built, older than 3 days", freed)
            continue
        if st["stale_needs_work"] and not st["open_pr"] and not st["live"]:
            if not _evict(d, dry_run, "only rejected/abandoned/stale changes", freed) and _newest_mtime(d) < now - 30 * 86400:
                _rm(d, dry_run, "only rejected/abandoned changes, untouched 30 days, uncommitted leftovers dropped", freed)
            continue
        # keep the clone; drop heavy rebuildable artifacts once they are old enough
        for h in HEAVY_DIRS:
            hp = d / h
            if hp.exists() and _newest_mtime(hp) < now - heavy_age_days * 86400:
                _rm(hp, dry_run, f"build artifact older than {heavy_age_days:g} days (rebuilt on demand)", freed)
        kept.append((_newest_mtime(d), d, "open PR" if st["open_pr"] else ("pending approval" if st["live"] else "recent")))

    # size cap: oldest clones without an open PR or pending change go first
    total = sum(_du_mb(p) for _, p, _ in kept if p.exists())
    if total / 1024 > max_ws:
        # biggest and stalest first: parking a 4 GB clone beats parking twenty small ones
        for mtime, p, why in sorted(kept, key=lambda k: -_du_mb(k[1]) * max(now - k[0], 3600)):
            if why == "locked" or mtime > now - 6 * 3600:
                continue      # in use, or touched in the last 6 hours
            size = _du_mb(p)
            if _evict(p, dry_run, f"workspace over {max_ws:g} GB cap (LRU, was: {why})", freed):
                total -= size
            if total / 1024 <= max_ws:
                break

    # project-local caches
    hub = CACHE / "hf" / "hub"
    if hub.exists():
        for m in hub.glob("models--*"):
            if _newest_mtime(m) < now - 3 * 86400:
                _rm(m, dry_run, "model weights downloaded by a test run, older than 3 days", freed)
    if (CACHE / "uv").exists() and _du_mb(CACHE / "uv") > 4096 and not dry_run:
        subprocess.run(["uv", "cache", "clean"], env={**os.environ, **isolated_env()}, capture_output=True)
        log.info(STAGE, "uv cache cleaned (was over 4 GB)")
    for sub in ("pip", "npm"):
        p = CACHE / sub
        if p.exists() and _du_mb(p) > 2048:
            _rm(p, dry_run, f"{sub} cache over 2 GB", freed)

    summary = {"freed_mb": sum(freed), "workspace_mb": _du_mb(WORKSPACE), "cache_mb": _du_mb(CACHE) if CACHE.exists() else 0,
               "free_gb": round(_free_gb(), 1), "dry_run": dry_run,
               "kept": [(p.name, why) for _, p, why in kept]}
    if summary["free_gb"] < min_free:
        log.warn(STAGE, f"only {summary['free_gb']} GB free after cleanup (want {min_free:g}); builds should be skipped")
    log.info(STAGE, f"freed {summary['freed_mb']} MB; workspace {summary['workspace_mb']} MB; free {summary['free_gb']} GB")
    return summary


def report() -> dict:
    _DU_CACHE.clear()
    out = {"free_gb": round(_free_gb(), 1), "workspace_mb": _du_mb(WORKSPACE) if WORKSPACE.exists() else 0,
           "cache_mb": _du_mb(CACHE) if CACHE.exists() else 0, "clones": []}
    if WORKSPACE.exists():
        for d in sorted(WORKSPACE.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                out["clones"].append({"name": d.name, "mb": _du_mb(d), "state": _clone_state(d.name.replace("__", "/", 1))})
    return out


# ---------------------------------------------------------------- parking: evict clones without losing work
PARKED = PROJECT / "data" / "parked"


def _git(d: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(d), capture_output=True, text=True, timeout=timeout)


def _default_branch(d: Path) -> str:
    r = _git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return r.stdout.strip().split("/", 1)[-1] if r.returncode == 0 and r.stdout.strip() else "main"


def _dirty(d: Path) -> bool:
    """Uncommitted tracked changes or new non-ignored files (ignored build output doesn't count)."""
    r = _git(d, "status", "--porcelain", "--ignore-submodules")
    return r.returncode != 0 or bool(r.stdout.strip())


def park(d: Path) -> bool:
    """Save every local work branch of clone `d` to a verified git bundle under data/parked/.
    True only if the clone can now be deleted without losing anything."""
    if not (d / ".git").exists():
        return False
    if _dirty(d):
        return False
    default = _default_branch(d)
    r = _git(d, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    branches = {b: sha for b, sha in (l.split() for l in r.stdout.splitlines() if l.strip()) if b != default}
    dest = PARKED / d.name
    dest.mkdir(parents=True, exist_ok=True)
    meta = {"repo": d.name.replace("__", "/", 1), "default": default, "branches": branches, "parked_at": time.time()}
    if branches:
        bundle = dest / "branches.bundle"
        tmp = dest / "branches.bundle.tmp"
        r = _git(d, "bundle", "create", str(tmp), *branches.keys(), f"^origin/{default}")
        if r.returncode != 0:
            # branch tips may be ancestors of the default branch (nothing to save) or history may be odd: bundle the tips whole
            r = _git(d, "bundle", "create", str(tmp), *branches.keys())
        if r.returncode != 0 or _git(d, "bundle", "verify", str(tmp)).returncode != 0:
            tmp.unlink(missing_ok=True)
            log.warn(STAGE, f"could not bundle {d.name}; keeping the clone: {r.stderr[-200:]}")
            return False
        # every branch tip must be inside the bundle
        heads = _git(d, "bundle", "list-heads", str(tmp)).stdout
        if not all(sha in heads for sha in branches.values()):
            tmp.unlink(missing_ok=True)
            log.warn(STAGE, f"bundle for {d.name} is missing a branch tip; keeping the clone")
            return False
        tmp.replace(bundle)
    (dest / "meta.json").write_text(json.dumps(meta, indent=1))
    return True


def restore(full_name: str) -> bool:
    """Re-create a parked clone with all its work branches. No-op if the clone exists or was never parked."""
    from .analyzer import ensure_clone, workspace_path
    d = workspace_path(full_name)
    dest = PARKED / d.name
    if d.exists() or not (dest / "meta.json").exists():
        return d.exists()
    meta = json.loads((dest / "meta.json").read_text())
    ensure_space(float(config.setting("MIN_FREE_GB")), keep={full_name})
    log.info(STAGE, f"restoring parked clone {full_name} ({len(meta['branches'])} branches)")
    ensure_clone(full_name, _restoring=True)
    bundle = dest / "branches.bundle"
    if meta["branches"] and bundle.exists():
        # the bundle's prerequisite commits must exist locally: fetch the bases the changes were built on
        for row in db.rows("SELECT DISTINCT base_sha FROM changes WHERE repo=? AND base_sha IS NOT NULL", (full_name,)):
            _git(d, "fetch", "-q", "--depth", "50", "origin", row["base_sha"], timeout=1800)
        for attempt in range(4):
            r = _git(d, "fetch", "-q", str(bundle), "refs/heads/*:refs/heads/*", timeout=1800)
            if r.returncode == 0:
                break
            _git(d, "fetch", "-q", "--deepen", str(500 * (attempt + 1)), "origin", timeout=1800)
        else:
            log.error(STAGE, f"could not restore branches for {full_name}: {r.stderr[-300:]}; bundle kept at {bundle}")
            return False
        got = _git(d, "for-each-ref", "--format=%(objectname)", "refs/heads").stdout
        if not all(sha in got for sha in meta["branches"].values()):
            log.error(STAGE, f"restored {full_name} but a branch tip is missing; bundle kept at {bundle}")
            return False
    shutil.rmtree(dest, ignore_errors=True)
    log.ok(STAGE, f"restored {full_name}")
    return True


def _evict(d: Path, dry: bool, why: str, freed: list[int]) -> bool:
    """Delete a clone only after its branches are safely parked."""
    if (d / LOCK).exists():
        return False
    if dry:
        _rm(d, True, why, freed)
        return True
    if not park(d):
        return False
    _rm(d, False, why + " (branches parked in data/parked)", freed)
    return True


_ALERTED: set[str] = set()


def ensure_space(need_gb: float, keep: set[str] | None = None) -> bool:
    """Make at least `need_gb` free, escalating only as far as needed. Never touches anything outside
    the project, locked clones, or the repos in `keep`. Returns False if it still can't get there."""
    if _free_gb() >= need_gb:
        return True
    keep = {k.replace("/", "__") for k in (keep or set())}
    log.warn(STAGE, f"{_free_gb():.1f} GB free, need {need_gb:g}; reclaiming")
    freed: list[int] = []
    # tier 1: normal cleanup
    cleanup(dry_run=False)
    if _free_gb() >= need_gb:
        return True
    # tier 2: every rebuildable artifact in every idle clone, and all project caches
    for d in sorted(WORKSPACE.iterdir()) if WORKSPACE.exists() else []:
        if d.is_dir() and not (d / LOCK).exists() and d.name not in keep:
            for h in HEAVY_DIRS:
                if (d / h).exists():
                    _rm(d / h, False, "low disk: rebuildable artifact", freed)
    if CACHE.exists():
        for sub in CACHE.iterdir():
            if sub.is_dir():
                _rm(sub, False, "low disk: project cache", freed)
        apply_isolation()      # recreate the empty cache dirs
    if _free_gb() >= need_gb:
        return True
    # tier 3: park and evict whole clones, least recently used first (open-PR clones included; they are restored on demand)
    clones = [d for d in WORKSPACE.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in keep and not (d / LOCK).exists()]
    for d in sorted(clones, key=lambda d: -_du_mb(d) * max(time.time() - _newest_mtime(d), 3600)):
        _evict(d, False, "low disk: LRU clone", freed)
        if _free_gb() >= need_gb:
            return True
    day = time.strftime("%Y-%m-%d")
    log.error(STAGE, f"still only {_free_gb():.1f} GB free after reclaiming {sum(freed)} MB inside the project")
    if day not in _ALERTED:
        _ALERTED.add(day)
        try:
            from .daily import send_digest
            send_digest(f"[oss-contrib] low disk: {_free_gb():.0f} GB free",
                        f"The engine cleared everything it safely could inside ~/oss-contrib and the Mac still has only "
                        f"{_free_gb():.1f} GB free (it needs {need_gb:g}). Builds pause until space frees up.\n\n"
                        f"Nothing outside ~/oss-contrib was touched. Biggest things outside it: {_outside_report()}")
        except Exception:
            pass
    return False


def _outside_report() -> str:
    """Sizes of common caches outside the project, for the alert email only. Read-only."""
    home = Path.home()
    out = []
    for p in (home / "Library/pnpm", home / "go", home / "Library/Caches/go-build", home / ".cargo/registry",
              home / ".npm", home / "Library/Caches/pip", home / ".cache/huggingface"):
        if p.exists():
            out.append(f"{p} {_du_mb(p, cached=False) // 1024} GB")
    return "; ".join(out) or "n/a"


if __name__ == "__main__":
    import sys
    if "--guard" in sys.argv:
        # watchdog (launchd): only acts when no pipeline run holds the lock, so it never pulls a clone out from under a build
        db.init()
        lock = PROJECT / "data" / "daily.lock"
        busy = False
        if lock.exists():
            try:
                os.kill(int(lock.read_text().strip() or 0), 0)
                busy = True
            except (ValueError, OSError):
                busy = False
        if busy:
            # A long run used to let the guard stand down completely, and free disk could slide well below
            # the floor before the run finished. cleanup() is safe to run alongside a build: it skips any
            # clone holding the per-build lock, and it parks every work branch into a verified git bundle
            # before deleting a clone, so nothing is lost and the active build is never touched. This keeps
            # the workspace under its cap even mid-run; whole-clone LRU parking of idle work still happens.
            s = cleanup(dry_run=False)
            print(json.dumps({"busy_reclaim": True, "freed_mb": s["freed_mb"], "free_gb": s["free_gb"]}))
        else:
            print(json.dumps({"ok": ensure_space(float(config.setting("MIN_FREE_GB")) + 8), "free_gb": round(_free_gb(), 1)}))
        sys.exit(0)
    dry = "--apply" not in sys.argv
    db.init()
    print(json.dumps(cleanup(dry_run=dry), indent=1)[:4000])
    if dry:
        print("\n(dry run; pass --apply to delete)")
