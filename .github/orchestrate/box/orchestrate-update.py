#!/usr/bin/env python3
"""Fast-forward the orchestrator's service checkout to origin/main before each tick (vws-deploy#490).

orchestrate-loop.sh runs this at the start of every tick, before orchestrate.py. It never blocks the
tick: whatever happens, the tick runs on whatever sha the checkout has afterwards.

- ORCHESTRATE_AUTO_UPDATE=0 (environment, or the env file, which wins) pins the current sha: no fetch,
  no fast-forward. The last fetched origin/main still feeds the drift check.
- Otherwise it fetches main from origin (timeout ORCHESTRATE_UPDATE_TIMEOUT, default 60 s) and runs
  `git merge --ff-only`. No reset, rebase or merge commit. It refuses, keeps the current sha and raises an
  alert when the checkout has local changes (tracked or untracked, outside the state dir), has diverged
  from origin/main, or the candidate fails a syntax check (bash -n on the box scripts, compile() on the
  Python entry points). A fetch or fast-forward failure also raises an alert.
- A service directory that is not a git checkout (an archive install) is result no_checkout, one alert.
- Drift: when the running sha is behind the newest commit on origin/main that touches WATCHED for two
  ticks in a row, one alert per that commit's sha.

Output: one JSON line (event "deploy") on stdout for loop.log, and STATE/deploy.json (0600, atomic) that
orchestrate.py renders into the status issue body: the running line, and each alert once per key.
Git's stderr is never printed (a remote URL may carry a credential); failures are logged by exit code.

Exit status: 3 when the fast-forward changed the loop's own files (.github/orchestrate/box/), so the loop
re-execs itself; 0 otherwise, including every refusal and failure."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

WATCHED = (".github/orchestrate", ".github/metrics", ".github/drift/repos.tsv")
LOOP_DIR = ".github/orchestrate/box/"
BASH_CHECK = (".github/orchestrate/box/orchestrate-loop.sh", ".github/orchestrate/box/orchestrate-ensure.sh")
PY_CHECK = (".github/orchestrate/orchestrate.py", ".github/orchestrate/box/orchestrate-update.py")
PY_OPTIONAL = (".github/metrics/metrics.py",)
MAIN_REF = "refs/remotes/origin/main"
EXIT_REEXEC = 3
DRIFT_TICKS = 2
GIT_TIMEOUT = 30

HERE = os.path.dirname(os.path.abspath(__file__))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(*parts):
    return "sha256:" + hashlib.sha256(":".join(("deploy",) + parts).encode()).hexdigest()


class Git:
    def __init__(self, cwd, fetch_timeout):
        self.cwd = cwd
        self.fetch_timeout = fetch_timeout
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        for k in ("GH_TOKEN", "GITHUB_TOKEN"):
            self.env.pop(k, None)

    def run(self, *args, timeout=GIT_TIMEOUT, stdin=None):
        """(exit code, stdout). stderr is dropped on purpose; 124 on timeout, 127 when git is missing."""
        try:
            r = subprocess.run(["git", "-C", self.cwd, *args], input=stdin, capture_output=True, text=True,
                               timeout=timeout, env=self.env)
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError:
            return 127, ""
        return r.returncode, r.stdout.strip()

    def sha(self, ref):
        rc, out = self.run("rev-parse", "--verify", "-q", ref + "^{commit}")
        return out if rc == 0 else ""

    def is_ancestor(self, a, b):
        return self.run("merge-base", "--is-ancestor", a, b)[0] == 0


def env_file_value(config, name):
    """The last NAME=0|1 line of the env file ('' when none); the env file wins over the environment."""
    path = os.path.join(config, "env")
    val = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line in (f"{name}=0", f"{name}=1"):
                    val = line[-1]
    except OSError:
        pass
    return val


def local_changes(git, root, state):
    """Paths with tracked or untracked changes, outside the state dir (when it lies inside the checkout)."""
    rc, out = git.run("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")
    if rc != 0:
        return ["<git status failed>"]
    rel_state = os.path.relpath(os.path.realpath(state), os.path.realpath(root))
    inside = not rel_state.startswith("..")
    paths = []
    for line in out.splitlines():
        p = line[3:].split(" -> ")[-1].strip('"')
        if inside and (p == rel_state or p.startswith(rel_state.rstrip("/") + "/")):
            continue
        paths.append(p)
    return paths


def candidate_problem(git, sha):
    """'' when the box scripts and Python entry points at sha pass a syntax check, else the first problem."""
    for rel in BASH_CHECK + PY_CHECK + PY_OPTIONAL:
        rc, src = git.run("show", f"{sha}:{rel}")
        if rc != 0:
            if rel in PY_OPTIONAL:
                continue
            return f"{rel} is missing"
        if rel.endswith(".sh"):
            try:
                r = subprocess.run(["bash", "-n"], input=src, capture_output=True, text=True, timeout=GIT_TIMEOUT)
            except (subprocess.TimeoutExpired, OSError):
                return f"{rel}: bash -n did not finish"
            if r.returncode != 0:
                return f"{rel}: bash -n failed"
        else:
            try:
                compile(src, rel, "exec")
            except (SyntaxError, ValueError):
                return f"{rel}: does not compile"
    return ""


def read_prev(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_atomic(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def update(root, state, enabled, fetch_timeout):
    """One update attempt. Returns the deploy record (see the module docstring)."""
    git = Git(root, fetch_timeout)
    head = git.sha("HEAD")
    rec = {"result": "current", "detail": "", "running": head, "previous": "", "main": "", "watched_main": "",
           "behind_ticks": 0, "reexec": False, "auto_update": "on" if enabled else "off", "alerts": []}
    if not enabled:
        rec["result"] = "disabled"
    else:
        rc, _ = git.run("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/main:{MAIN_REF}",
                        timeout=fetch_timeout)
        if rc != 0:
            rec.update(result="fetch_failed", detail=f"git fetch exited {rc}")
    main = git.sha(MAIN_REF)
    rec["main"] = main
    if rec["result"] == "current":
        if not main:
            rec.update(result="fetch_failed", detail=f"{MAIN_REF} is missing")
        elif main == head:
            pass
        elif not git.is_ancestor(head, main):
            rec.update(result="refused", detail="the checkout has diverged from origin/main")
        else:
            changes = local_changes(git, root, state)
            problem = "" if changes else candidate_problem(git, main)
            if changes:
                rec.update(result="refused", detail=f"local changes in {len(changes)} path(s), first {changes[0]}")
            elif problem:
                rec.update(result="refused", detail=f"candidate {main[:7]} failed a check: {problem}")
            else:
                rc, _ = git.run("merge", "--ff-only", "--quiet", MAIN_REF, timeout=fetch_timeout)
                new = git.sha("HEAD")
                if rc != 0 or new != main:
                    rec.update(result="ff_failed", detail=f"git merge --ff-only exited {rc}", running=new or head)
                else:
                    rc, names = git.run("diff", "--name-only", head, new)
                    rec.update(result="updated", running=new, previous=head,
                               reexec=rc != 0 or any(n.startswith(LOOP_DIR) for n in names.splitlines()))
    if rec["result"] in ("refused", "fetch_failed", "ff_failed"):
        rec["alerts"].append({"kind": rec["result"], "key": key(rec["result"], rec["running"], rec["detail"]),
                              "text": f"auto-update {rec['result']} on `{rec['running'][:7]}`: {rec['detail']}; "
                                      "the tick runs the current sha"})
    if main:
        rc, watched = git.run("log", "-1", "--format=%H", main, "--", *WATCHED)
        rec["watched_main"] = watched if rc == 0 else ""
    behind = bool(rec["watched_main"]) and not git.is_ancestor(rec["watched_main"], rec["running"])
    prev = read_prev(os.path.join(state, "deploy.json"))
    same = prev.get("watched_main") == rec["watched_main"]
    rec["behind_ticks"] = (int(prev.get("behind_ticks", 0) or 0) if same else 0) + 1 if behind else 0
    if rec["behind_ticks"] >= DRIFT_TICKS:
        w = rec["watched_main"]
        rec["alerts"].append({"kind": "drift", "key": key("drift", w),
                              "text": f"running `{rec['running'][:7]}` is behind `{w[:7]}` on main (changes "
                                      f".github/orchestrate or the metrics scripts) for {DRIFT_TICKS} ticks or more; "
                                      f"auto-update is {rec['auto_update']}, last result {rec['result']}"})
    return rec


def main(environ=None):
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~")
    state = environ.get("ORCHESTRATE_STATE_DIR") or os.path.join(home, ".local", "state", "vws-orchestrate")
    config = environ.get("ORCHESTRATE_CONFIG_DIR") or os.path.join(home, ".config", "vws-orchestrate")
    os.makedirs(state, mode=0o700, exist_ok=True)
    flag = env_file_value(config, "ORCHESTRATE_AUTO_UPDATE") or environ.get("ORCHESTRATE_AUTO_UPDATE", "1")
    try:
        timeout = max(1, int(environ.get("ORCHESTRATE_UPDATE_TIMEOUT", "60")))
    except ValueError:
        timeout = 60
    try:
        rc, root = Git(HERE, timeout).run("rev-parse", "--show-toplevel")
        inside = rc == 0 and root and os.path.realpath(os.path.join(root, LOOP_DIR)) == os.path.realpath(HERE)
        if not inside:  # not a clone, or an archive extracted inside some other repository
            rec = {"result": "no_checkout", "detail": "the service directory is not a git checkout", "running": "",
                   "previous": "", "main": "", "watched_main": "", "behind_ticks": 0, "reexec": False,
                   "auto_update": "off" if flag == "0" else "on",
                   "alerts": [{"kind": "no_checkout", "key": key("no_checkout", HERE),
                               "text": "the service directory is not a git checkout; auto-update cannot run"}]}
        else:
            rec = update(root, state, flag != "0", timeout)
    except Exception as e:  # noqa: BLE001 - an update bug must never stop the tick
        rec = {"result": "error", "detail": type(e).__name__, "running": "", "previous": "", "main": "",
               "watched_main": "", "behind_ticks": 0, "reexec": False, "auto_update": "on" if flag != "0" else "off",
               "alerts": []}
    rec["at"] = now_iso()
    try:
        write_atomic(os.path.join(state, "deploy.json"), rec)
    except OSError as e:
        rec["detail"] = (rec["detail"] + f"; deploy.json not written ({type(e).__name__})").lstrip("; ")
    line = {"ts": rec["at"], "event": "deploy", "result": rec["result"], "running": rec["running"][:7],
            "main": rec["main"][:7], "previous": rec["previous"][:7], "reexec": rec["reexec"],
            "behind_ticks": rec["behind_ticks"], "auto_update": rec["auto_update"], "detail": rec["detail"],
            "alerts": [a["kind"] for a in rec["alerts"]]}
    print(json.dumps(line, sort_keys=True), flush=True)
    return EXIT_REEXEC if rec["reexec"] else 0


if __name__ == "__main__":
    sys.exit(main())
