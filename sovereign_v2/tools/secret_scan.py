"""Find current secret VALUES in files or git history. Prints paths and secret
NAMES, never values.

    secret_scan.py tree DIR [DIR...]      working trees (tracked or not)
    secret_scan.py history REPO           every blob ever committed (git log -p --all)

Secrets are read from ~/.coalition/secrets.env and ~/market-analysis-agent/.envrc
at runtime. A value counts if it is >= 8 chars and not an obvious non-secret
(boolean, number, path, bare URL, model name). Names are graded: a hit on
something called *KEY/*SECRET/*TOKEN/*PASS* is a LEAK; a hit on any other
variable is reported as a config value, so a noisy match cannot hide a real one.

Pattern hits (Alpaca/Anthropic/GitHub/AWS/Slack key shapes, private-key
headers) are reported too, for secrets that are not in either env file.

Exit 1 when any LEAK or pattern hit is found, so a caller can gate on it.
"""
import os
import pathlib
import re
import subprocess
import sys

HOME = pathlib.Path.home()
ENV_FILES = [HOME / ".coalition/secrets.env", HOME / "market-analysis-agent/.envrc"]
SECRETISH = re.compile(r"KEY|SECRET|TOKEN|PASS|PWD|AUTH|CRED|PRIVATE|WEBHOOK|DSN|COOKIE|SESSION", re.I)
NON_SECRET = re.compile(r"^(true|false|yes|no|none|null|\d+(\.\d+)?|/[\w./-]*|~[\w./-]*|https?://[^@\s]*|claude-[\w.-]+|gpt-[\w.-]+|qwen[\w.:-]*)$", re.I)
PATTERNS = {
    "alpaca-key-id": r"\b(?:PK|AK)[A-Z0-9]{18,}\b",
    "anthropic-key": r"sk-ant-[A-Za-z0-9_-]{40,}",
    "github-token": r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{30,}",
    "aws-key-id": r"\bAKIA[A-Z0-9]{16}\b",
    "slack-token": r"\bxox[abpr]-[0-9A-Za-z-]{10,}",
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
}
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", "sovereign_state", "sovereign_results", "dist", "build"}
MAX_BYTES = 2_000_000


def load_secrets():
    vals = {}
    for p in ENV_FILES:
        if not p.exists():
            continue
        for ln in p.read_text(errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", ln)
            if not m:
                continue
            name, v = m.group(1), m.group(2).strip().strip("\"'")
            if len(v) >= 8 and not NON_SECRET.match(v):
                vals.setdefault(v, set()).add(name)
    return vals


def scan_text(text, where, secrets, out):
    for v, names in secrets.items():
        if v in text:
            for i, ln in enumerate(text.splitlines(), 1):
                if v in ln:
                    grade = "LEAK" if any(SECRETISH.search(n) for n in names) else "config"
                    out.append((grade, where, i, ",".join(sorted(names))))
    for label, pat in PATTERNS.items():
        for m in re.finditer(pat, text):
            if m.group(0) in secrets:
                continue  # already reported by value, with its name
            line = text.count("\n", 0, m.start()) + 1
            out.append(("PATTERN", where, line, label))


def scan_tree(dirs, secrets):
    out = []
    for d in dirs:
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [s for s in subdirs if s not in SKIP_DIRS]
            for f in files:
                p = pathlib.Path(root) / f
                try:
                    if p.is_symlink() or p.stat().st_size > MAX_BYTES:
                        continue
                    b = p.read_bytes()
                except OSError:
                    continue
                if b"\x00" in b[:4096]:
                    continue  # binary
                scan_text(b.decode("utf-8", "replace"), str(p), secrets, out)
    return out


def scan_history(repo, secrets):
    """Stream every committed diff; attribute hits to commit + file."""
    out = []
    proc = subprocess.Popen(["git", "-C", repo, "log", "-p", "--all", "--format=@@COMMIT %h %ad", "--date=short"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace")
    commit, path, buf = "?", "?", []

    def flush():
        if buf:
            scan_text("\n".join(buf), f"{commit} {path}", secrets, out)
            buf.clear()

    for ln in proc.stdout:
        if ln.startswith("@@COMMIT "):
            flush()
            commit = ln[9:].strip()
        elif ln.startswith("+++ b/"):
            flush()
            path = ln[6:].strip()
        elif ln.startswith("+") and not ln.startswith("+++"):
            buf.append(ln[1:].rstrip("\n"))
    flush()
    proc.wait()
    return out


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("tree", "history"):
        sys.exit(__doc__)
    secrets = load_secrets()
    names = sorted({n for ns in secrets.values() for n in ns})
    print(f"  checking {len(secrets)} secret values ({len(names)} names) + {len(PATTERNS)} key shapes")
    hits = scan_tree(sys.argv[2:], secrets) if sys.argv[1] == "tree" else scan_history(sys.argv[2], secrets)
    seen = set()
    for grade, where, line, what in sorted(hits):
        key = (grade, where, what)
        if key in seen:
            continue
        seen.add(key)
        print(f"  {grade:8s} {where}:{line}  [{what}]")
    bad = sum(1 for g, *_ in hits if g in ("LEAK", "PATTERN"))
    print(f"  => {bad} leak/pattern hit(s), {sum(1 for g, *_ in hits if g == 'config')} config-value hit(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
