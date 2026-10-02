"""Behaviour tests for deploy.sh and setup.sh, run without deploying anything.

Template from the rw-coding-compliance skill (deployment standard v6). The skill's copy
is the canonical one: copy it to the repo's tests/, adjust only the block marked
"Adjust for this repo", and run it:

    python3 tests/test_deploy_script.py
    pytest tests/test_deploy_script.py

If a case needs more than the settings block to pass against a script that follows the
standard, that is a defect in this template: fix it in the skill rather than in the
repo's copy, so every repo gets the fix.

Each case copies the real deploy.sh into a throwaway git repo with a bare origin, puts
`docker`, `curl` and `sleep` shims first on PATH, and runs it. Nothing is built, started
or pushed anywhere real. The cases pin the rules whose plausible-looking wrong
implementation passes a grep:

- git.self-update-handover: a git section that rewrites deploy.sh (by merge, reset or
  checkout) must hand over to the new copy and carry OLD_HEAD across, or a dependency
  change quietly skips --no-cache;
- git.reset-guard: the reset must not destroy unpushed commits on main, hand edits to
  tracked files, or detached commits, and must not refuse when nothing is at risk;
- git.base-sync: a run with nothing to merge still deploys origin's main;
- fail.silent-abort: a failure names its stage, nothing reports success after it, and a
  successful run prints no failure line;
- fail.guarded-reads: a .env missing a key is the normal case, not a silent abort, and a
  large dependency diff still rebuilds (grep -q at the end of a pipe misses it).
"""
import os, re, shlex, shutil, subprocess, sys, tempfile

# ── Adjust for this repo ──────────────────────────────────────────────────────
# A .env the deploy accepts. Include every key deploy.sh needs to have a value.
ENV_TEXT = "PLEX_TOKEN=tok\nPLEX_BASE_URL=http://127.0.0.1:32400\nPORT=8300\nCACHE_TTL=300\n"
# A .env that lacks the optional keys, to prove a missing key is not an abort.
# Keep only what deploy.sh genuinely cannot run without.
ENV_MINIMAL = "PLEX_TOKEN=tok\n"
# Repo files deploy.sh reads besides itself (compose file, config it greps, ...).
EXTRA_FILES = []
# What the curl shim prints, for health checks that inspect the body.
CURL_OUTPUT = '{"status":"ok"}'
# What the docker shim prints, by argument prefix (first match wins). For health
# checks that read compose's own output rather than curl, e.g.
#   "compose ps --status running --services": "app\nbackup\n",
#   "compose logs backup": "backup-1  | backup started: ...\n",
DOCKER_OUTPUT = {}
# The base branch deploy.sh merges into.
BASE = "main"
# ──────────────────────────────────────────────────────────────────────────────

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="deployscript-")
MARKER = "MERGED-COPY-MARKER"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}
# A build step, however it is spelled: `docker compose build`, `docker-compose build`,
# or through the `$COMPOSE` variable. Mirrors COMPOSE_CMD in scan_conformance.sh.
BUILD_LINE = re.compile(r"(docker\s+compose|docker-compose|\$\{?COMPOSE\}?)\s+build\b")


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout.strip()


def first_error(out):
    """The first ✗ line and what follows it: one cause, not eighty lines of run."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "✗" in line:
            return "\n".join(lines[i:i + 3])
    return "\n".join(lines[-5:])


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}"
          f"{'  — ' + detail if detail and not condition else ''}")
    return condition


def make_shims(fail_build=False):
    """docker logs its arguments and succeeds; curl prints CURL_OUTPUT; sleep returns
    at once, so a health check that never passes fails the case in milliseconds."""
    bindir = tempfile.mkdtemp(dir=ROOT, prefix="bin-")
    log = os.path.join(bindir, "docker.log")
    build_exit = "exit 1" if fail_build else "true"
    cases = "".join(f'  "{prefix}"*) printf %s {shlex.quote(out)} ;;\n'
                    for prefix, out in DOCKER_OUTPUT.items())
    with open(f"{bindir}/docker", "w") as f:
        f.write(f'#!/bin/sh\necho "$*" >> "{log}"\n'
                f'case "$*" in\n  "compose build"*) {build_exit} ;;\n{cases}esac\n')
    with open(f"{bindir}/curl", "w") as f:
        f.write(f"#!/bin/sh\necho '{CURL_OUTPUT}'\n")
    with open(f"{bindir}/sleep", "w") as f:
        f.write("#!/bin/sh\nexit 0\n")
    for name in ("docker", "curl", "sleep"):
        os.chmod(f"{bindir}/{name}", 0o755)
    return bindir, log


def make_repo(name, env_text=ENV_TEXT):
    """A checkout of BASE carrying the real deploy.sh, pushed to a bare origin.

    requirements.txt is always created: the dependency-diff pattern matches it
    whatever manifests the real app uses, so cases can change "a dependency".
    """
    base = os.path.join(ROOT, name)
    origin, work = base + "-origin.git", base
    subprocess.run(["git", "init", "-q", "--bare", origin], check=True)
    subprocess.run(["git", "init", "-q", work], check=True)
    shutil.copy2(os.path.join(REPO, "deploy.sh"), work)
    for rel in EXTRA_FILES:
        os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
        shutil.copy2(os.path.join(REPO, rel), os.path.join(work, rel))
    open(f"{work}/requirements.txt", "a").write("")
    open(f"{work}/.gitignore", "w").write(".env\n")
    sh(work, "git", "add", "-A"); sh(work, "git", "commit", "-qm", "initial")
    sh(work, "git", "branch", "-M", BASE)
    sh(work, "git", "remote", "add", "origin", origin)
    sh(work, "git", "push", "-q", "-u", "origin", BASE)
    open(f"{work}/.env", "w").write(env_text)
    return work


def behind(name):
    """A checkout whose BASE is one commit behind origin: a PR merged on GitHub since
    the last deploy changed deploy.sh (marker) and a dependency. Returns the checkout
    and a second clone that pushed the change."""
    work = make_repo(name)
    other = work + "-other"
    sh(ROOT, "git", "clone", "-q", "-b", BASE, work + "-origin.git", other)
    open(f"{other}/deploy.sh", "a").write(f'\necho "{MARKER}"\n')
    open(f"{other}/requirements.txt", "a").write("newdep\n")
    sh(other, "git", "commit", "-qam", "merged on GitHub")
    sh(other, "git", "push", "-q", "origin", BASE)
    return work, other


def run(work, bindir, *args):
    """stdout and stderr interleaved, so 'nothing after the error' is checkable."""
    p = subprocess.run(["./deploy.sh", *args], cwd=work, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                       env={**os.environ, **GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}"})
    return p.stdout, p.returncode


def docker_log(log):
    return open(log).read() if os.path.exists(log) else ""


def build_stage():
    """The header deploy.sh prints before its build step, or None when no build line
    is found, so case 7 fails saying so rather than blaming whatever header came last."""
    stage = None
    for line in open(os.path.join(REPO, "deploy.sh")):
        m = re.match(r'\s*header\s+"([^"]+)"', line)
        if m:
            stage = m.group(1)
        if BUILD_LINE.search(line) and not line.lstrip().startswith("#"):
            return stage
    return None


def main():
    ok = True
    scripts = [s for s in ("deploy.sh", "setup.sh") if os.path.exists(os.path.join(REPO, s))]
    try:
        # 1. every script parses
        for script in scripts:
            p = subprocess.run(["bash", "-n", os.path.join(REPO, script)], capture_output=True)
            ok &= check(f"1. {script} passes bash -n", p.returncode == 0, p.stderr.decode().strip())

        # 2. fail.guarded-reads: no assignment from a bare grep, and no grep feeding a
        #    process substitution without a guard (under -E a no-match there fires the trap)
        for script in scripts:
            bad = [l.strip() for l in open(os.path.join(REPO, script))
                   if not l.lstrip().startswith("#")
                   and (re.match(r"^\s*\w+=\$\(.*\bgrep\b", l) or re.search(r"<\s*<\(.*\bgrep\b", l))
                   and "||" not in l]
            ok &= check(f"2. {script} has no unguarded grep read", not bad, "; ".join(bad))

        # 3. fail.silent-abort: the trap names the stage. From a file, not `bash -c`,
        #    because the preamble reads BASH_SOURCE.
        for script in scripts:
            lines = open(os.path.join(REPO, script)).read().splitlines()
            end = next((i for i, l in enumerate(lines) if l.startswith("trap ")), None)
            if end is None:
                ok &= check(f"3. {script} has an ERR trap", False, "no top-level `trap` line")
                continue
            probe = os.path.join(ROOT, f"probe-{script}")
            open(probe, "w").write("\n".join(lines[:end + 1] +
                                   ['header "Probe stage"', "false", "echo NOT-REACHED"]) + "\n")
            p = subprocess.run(["bash", probe], capture_output=True, text=True, cwd=ROOT)
            ok &= check(f"3. {script} ERR trap names the stage",
                        "Probe stage" in p.stderr and "NOT-REACHED" not in p.stdout
                        and p.returncode != 0, f"exit={p.returncode} stderr={p.stderr.strip()!r}")

        # 4. args.unknown-args / args.branch-flag: refused before any work
        work = make_repo("args")
        bindir, log = make_shims()
        out, code = run(work, bindir, "--nonsense")
        ok &= check("4. unknown argument fails", code != 0, out.strip())
        out, code = run(work, bindir, "--branch")
        ok &= check("4b. --branch without a value fails", code != 0, out.strip())
        # A read-only probe such as `docker compose version` is not work; a deploy step is.
        steps = [l for l in docker_log(log).splitlines()
                 if re.match(r"compose (build|up|exec|down|pull|run)\b", l)]
        ok &= check("4c. neither reached a deploy step", not steps, "; ".join(steps))

        # 5. git.self-update-handover: a merge that rewrites deploy.sh (and a
        #    dependency) is finished by the merged copy, still with --no-cache
        work = make_repo("handover")
        bindir, log = make_shims()
        sh(work, "git", "checkout", "-qb", "claude/change")
        # The marker goes last, so a script ending in `exit` needs it moved above that.
        open(f"{work}/deploy.sh", "a").write(f'\necho "{MARKER}"\n')
        open(f"{work}/requirements.txt", "a").write("newdep\n")
        sh(work, "git", "commit", "-qam", "change deploy.sh and a dependency")
        sh(work, "git", "push", "-q", "origin", "claude/change")
        sh(work, "git", "checkout", "-q", BASE); sh(work, "git", "branch", "-qD", "claude/change")
        out, code = run(work, bindir, "--branch", "claude/change")
        docker = docker_log(log)
        ok &= check("5. deploy succeeds", code == 0, f"exit={code}\n{first_error(out)}")
        ok &= check("5b. the merged copy finished the run", MARKER in out)
        ok &= check("5c. the resumed run says so", "Continuing as the merged" in out,
                    "no 'Continuing as the merged …' line: is the DEPLOY_RESUMED test "
                    "reachable on the --skip-git path?")
        ok &= check("5d. OLD_HEAD survived: the dependency change still builds --no-cache",
                    "--no-cache" in docker, docker.strip())

        # 6. a plain --skip-git with nothing changed neither hands over nor drops the cache
        bindir, log = make_shims()
        out, code = run(work, bindir, "--skip-git")
        docker = docker_log(log)
        ok &= check("6. --skip-git deploys without handing over",
                    code == 0 and "Continuing as the merged" not in out, f"exit={code}\n{first_error(out)}")
        ok &= check("6b. and keeps the build cache", "--no-cache" not in docker, docker.strip())

        # 7. fail.silent-abort + the failure contract: a failed build names its stage,
        #    exits non-zero, and nothing reports success after it
        bindir, log = make_shims(fail_build=True)
        out, code = run(work, bindir, "--skip-git")
        stage = build_stage()
        after = out.split("✗", 1)[1] if "✗" in out else out
        ok &= check("7. failed build exits non-zero", code != 0, f"exit={code}")
        ok &= check(f"7b. and names the stage ({stage!r})", bool(stage) and stage in after,
                    "no build line found in deploy.sh" if not stage else first_error(out))
        ok &= check("7c. and prints no success line after the failure", "✓" not in after, after)

        # 8. fail.guarded-reads, behaviourally: optional keys absent is not an abort
        work = make_repo("minimal-env", env_text=ENV_MINIMAL)
        bindir, log = make_shims()
        out, code = run(work, bindir, "--skip-git")
        ok &= check("8. a .env without optional keys still deploys", code == 0, f"exit={code}\n{first_error(out)}")

        # 9. git.reset-guard: a host left on another branch must not lose unpushed
        #    commits on local BASE to the reset. The v5 guard measured HEAD, not BASE.
        work = make_repo("reset-guard")
        bindir, log = make_shims()
        open(f"{work}/local-only.txt", "w").write("x\n")
        sh(work, "git", "add", "local-only.txt"); sh(work, "git", "commit", "-qm", "unpushed on base")
        sh(work, "git", "checkout", "-qb", "elsewhere", f"origin/{BASE}")
        out, code = run(work, bindir)
        kept = sh(work, "git", "log", "--format=%s", BASE)
        ok &= check("9. refuses when local base has unpushed commits", code != 0, f"exit={code}")
        ok &= check("9b. and the commit survives on base", "unpushed on base" in kept, kept)
        ok &= check("9c. and nothing was deployed", "compose up" not in docker_log(log))

        # 10. git.reset-guard: a hand edit to a tracked file is refused, not wiped
        work = make_repo("dirty")
        bindir, log = make_shims()
        open(f"{work}/requirements.txt", "a").write("hand-edit\n")
        out, code = run(work, bindir)
        ok &= check("10. refuses with an uncommitted edit to a tracked file", code != 0, f"exit={code}")
        ok &= check("10b. and the edit is intact", "hand-edit" in open(f"{work}/requirements.txt").read())
        ok &= check("10c. and the refusal names the file", "requirements.txt" in out, first_error(out))

        # 11. git.reset-guard: no false refusal for a local branch with its own commits
        work = make_repo("no-false-refusal")
        bindir, log = make_shims()
        sh(work, "git", "checkout", "-qb", "scratch")
        open(f"{work}/scratch.txt", "w").write("x\n")
        sh(work, "git", "add", "scratch.txt"); sh(work, "git", "commit", "-qm", "scratch work")
        tip = sh(work, "git", "rev-parse", "scratch")
        out, code = run(work, bindir)
        ok &= check("11. deploys when base is clean and another branch has work", code == 0,
                    f"exit={code}\n{first_error(out)}")
        ok &= check("11b. and that branch is untouched", sh(work, "git", "rev-parse", "scratch") == tip)

        # 12. git.reset-guard: a detached commit no branch holds is not orphaned
        work = make_repo("detached")
        bindir, log = make_shims()
        sh(work, "git", "checkout", "-q", "--detach")
        open(f"{work}/orphan.txt", "w").write("x\n")
        sh(work, "git", "add", "orphan.txt"); sh(work, "git", "commit", "-qm", "detached work")
        orphan = sh(work, "git", "rev-parse", "HEAD")
        out, code = run(work, bindir)
        ok &= check("12. refuses to switch away from an unheld detached commit", code != 0, f"exit={code}")
        ok &= check("12b. and HEAD still holds it", sh(work, "git", "rev-parse", "HEAD") == orphan)

        # 13. git.self-update-handover after a rollback: the host sits detached on an older
        #     commit, so `git checkout main` is what rewrites deploy.sh. A baseline
        #     captured after that checkout never hands over.
        work = make_repo("rollback")
        open(f"{work}/deploy.sh", "a").write(f'\necho "{MARKER}"\n')
        open(f"{work}/requirements.txt", "a").write("newdep\n")
        sh(work, "git", "commit", "-qam", "newer main"); sh(work, "git", "push", "-q", "origin", BASE)
        sh(work, "git", "checkout", "-q", "--detach", f"{BASE}~1")
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("13. rollback host: deploy succeeds", code == 0, f"exit={code}\n{first_error(out)}")
        ok &= check("13b. the checkout's rewrite of deploy.sh is handed over",
                    MARKER in out and "Continuing as the merged" in out,
                    "is OLD_HEAD captured after the checkout?")
        ok &= check("13c. and its dependency change builds --no-cache", "--no-cache" in docker_log(log))

        # 14. behind origin, with a branch to merge: the reset that catches main up
        #     rewrites deploy.sh and a dependency, and both must be honoured
        work, other = behind("behind-merge")
        sh(other, "git", "checkout", "-qb", "claude/next")
        open(f"{other}/feature.txt", "w").write("x\n")
        sh(other, "git", "add", "feature.txt"); sh(other, "git", "commit", "-qm", "feature")
        sh(other, "git", "push", "-q", "origin", "claude/next")
        bindir, log = make_shims()
        out, code = run(work, bindir, "--branch", "claude/next")
        ok &= check("14. behind origin, with a branch to merge: deploy succeeds", code == 0,
                    f"exit={code}\n{first_error(out)}")
        ok &= check("14b. the reset's rewrite of deploy.sh is handed over", MARKER in out,
                    "is OLD_HEAD re-captured after the reset?")
        ok &= check("14c. and its dependency change builds --no-cache", "--no-cache" in docker_log(log))

        # 15. git.base-sync: behind origin with nothing to merge still deploys origin's main,
        #     and a successful run prints no failure line
        work, _ = behind("behind-idle")
        bindir, log = make_shims()
        out, code = run(work, bindir)
        ok &= check("15. behind origin, nothing to merge: deploy succeeds", code == 0,
                    f"exit={code}\n{first_error(out)}")
        ok &= check("15b. and deploys origin's main, not the stale checkout",
                    sh(work, "git", "rev-parse", "HEAD") == sh(work, "git", "rev-parse", f"origin/{BASE}")
                    and MARKER in out, "is the reset inside the merge branch only?")
        # With no claude/* branch, branch discovery finds nothing. Under -E an unguarded
        # ERR trap reports a no-match grep from inside the <(…) subshell, and the run
        # goes on to succeed underneath a "Deploy failed" line.
        ok &= check("15c. and prints no failure line", "✗" not in out,
                    "does on_err report from subshells? guard it on BASH_SUBSHELL")

        # 16. fail.guarded-reads: a large merge still rebuilds. `grep -q` ending the
        #     dependency-diff pipe exits at its first match, git dies of SIGPIPE, and
        #     under pipefail the match reads as a miss. Needs output past the pipe buffer.
        work = make_repo("large-diff")
        sh(work, "git", "checkout", "-qb", "claude/big")
        open(f"{work}/requirements.txt", "a").write("bigdep\n")
        os.makedirs(f"{work}/zz-vendored")
        for i in range(4000):
            open(f"{work}/zz-vendored/a-fairly-long-vendored-file-name-{i:05d}.txt", "w").write("x")
        sh(work, "git", "add", "-A"); sh(work, "git", "commit", "-qm", "large change")
        sh(work, "git", "push", "-q", "origin", "claude/big")
        sh(work, "git", "checkout", "-q", BASE); sh(work, "git", "branch", "-qD", "claude/big")
        bindir, log = make_shims()
        out, code = run(work, bindir, "--branch", "claude/big")
        ok &= check("16. a large merge deploys", code == 0, f"exit={code}\n{first_error(out)}")
        ok &= check("16b. and its dependency change still builds --no-cache",
                    "--no-cache" in docker_log(log), "does the dependency diff end in grep -q?")
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)

    print("\nRESULT:", "all cases pass" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


def test_deploy_script():
    """pytest entry point; the assertions print their own PASS/FAIL lines."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    sys.exit(main())
