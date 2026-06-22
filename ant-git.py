#!/usr/bin/env python3
"""ant-git: bidirectional converter between ~ANT colonies and git repos.

usage:
    ./ant-git.py ant2git <project_dir> <git_dir>
    ./ant-git.py git2ant <git_dir> <project_dir>

ant2git reads .ant/ from <project_dir>, creates a git repo at <git_dir>
with equivalent history, branches, and working tree.

git2ant reads a git repo at <git_dir>, creates <project_dir>/.ant/ with
equivalent history and restores the working tree.

limitations:
    - merge commits: ANT tracks only one parent (first-parent in git)
    - file permissions not preserved (ANT doesn't track them)
    - git commit timestamps are not preserved in ant2git direction
      (ANT chambers have no timestamp field on disk)

file deletion:
    ~ANT records a removal as a "tombstone" manifest entry: a filename
    prefixed with BURY_MARKER (".ant-bury/") backed by an empty blob. The
    cumulative-snapshot walk treats a tombstone as "name seen, but absent",
    so the file drops out of the snapshot. git2ant emits delta chambers
    (only files that changed vs the first parent) plus a tombstone for each
    deleted path; ant2git omits tombstoned files from the git tree.
"""

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# tombstone marker: a manifest entry of "<BURY_MARKER><path>" records a removal
BURY_MARKER = ".ant-bury/"


@dataclass
class Chamber:
    id: int
    parent: int
    message: str
    name: str = ""
    files: list = field(default_factory=list)
    blobs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ANT parsing
# ---------------------------------------------------------------------------

def parse_chambers(ant_dir: Path) -> dict:
    chambers = {}
    cdir = ant_dir / "chambers"
    if not cdir.exists():
        return chambers
    for entry in sorted(cdir.iterdir(), key=lambda p: int(p.name)):
        cid = int(entry.name)
        parent = int((entry / "parent").read_text().strip())
        message = ""
        if (entry / "message").exists():
            message = (entry / "message").read_text().strip()
        name = ""
        if (entry / "name").exists():
            name = (entry / "name").read_text().strip()
        files = []
        if (entry / "files").exists():
            raw = (entry / "files").read_text()
            if raw.strip():
                files = raw.strip().split("\n")
        blobs = {}
        for i in range(len(files)):
            bp = entry / str(i)
            if bp.exists():
                blobs[i] = bp.read_bytes()
        chambers[cid] = Chamber(cid, parent, message, name, files, blobs)
    return chambers


def parse_refs(ant_dir: Path) -> dict:
    refs = {}
    rd = ant_dir / "refs"
    if rd.exists():
        for f in rd.iterdir():
            refs[f.name] = int(f.read_text().strip())
    return refs


def parse_head(ant_dir: Path) -> str:
    return (ant_dir / "HEAD").read_text().strip()


def cumulative_snapshot(chambers: dict, tip: int) -> dict:
    """Walk parent chain from tip, first-occurrence-wins → {filename: bytes}.

    A tombstone entry (BURY_MARKER + path) shadows the real name without
    adding it to the snapshot, so buried files disappear (just like the
    ~ATH cumulative walk).
    """
    snap = {}
    seen = set()
    cur = tip
    while cur != 0 and cur in chambers:
        ch = chambers[cur]
        for i, fname in enumerate(ch.files):
            if fname.startswith(BURY_MARKER):
                real = fname[len(BURY_MARKER):]
                seen.add(real)  # shadow ancestors; do not add to snapshot
                continue
            if fname not in seen:
                seen.add(fname)
                if i in ch.blobs:
                    snap[fname] = ch.blobs[i]
        cur = ch.parent
    return snap


def ancestors(chambers: dict, start: int) -> set:
    """Collect all chamber ids on the parent chain from start to root."""
    acc = set()
    cur = start
    while cur != 0 and cur in chambers:
        acc.add(cur)
        cur = chambers[cur].parent
    return acc


SPLICE_RE = re.compile(r"^splice tunnel '(.+)'$")
MERGE_MSG_RE = re.compile(r"Merge (?:branch )?'(.+?)'")
CHAMBER_NAME_RE = re.compile(r"^chamber-name: (.+)$", re.MULTILINE)


def detect_splice(message: str):
    """If message matches ANT splice pattern, return source tunnel name."""
    m = SPLICE_RE.match(message)
    return m.group(1) if m else None


def find_theirs_parent(chambers: dict, splice_id: int, ours_parent: int,
                       source_tip) -> int | None:
    """Find the THEIRS parent chamber for a splice commit.

    Walk backwards from source_tip. The THEIRS parent is the first
    chamber with id < splice_id that is NOT an ancestor of ours_parent
    (i.e. it belongs exclusively to the source branch).
    """
    if source_tip is None:
        return None
    ours_anc = ancestors(chambers, ours_parent)
    cur = source_tip
    while cur != 0 and cur in chambers:
        if cur < splice_id and cur not in ours_anc:
            return cur
        cur = chambers[cur].parent
    return None


def is_ancestor(git_dir, a: str, b: str) -> bool:
    """True if commit a is an ancestor of (or equal to) commit b."""
    try:
        git(git_dir, "merge-base", "--is-ancestor", a, b)
        return True
    except RuntimeError:
        return False


def resolve_merge_source(git_dir, message: str, first_parent,
                         second_parent: str, branches: dict) -> str:
    """Determine source branch name for a git merge commit.

    Priority:
      1. explicit git merge subject ("Merge branch 'X'")
      2. round-tripped ANT subject ("splice tunnel 'X'") — keeps
         ant->git->ant faithful, since ant2git preserves this message
      3. branch topology: the source branch is one whose history contains
         the THEIRS parent but whose tip is not already folded into the
         OURS parent (which would mark it as the target / an already-merged
         branch). A branch whose tip *is* the THEIRS parent wins outright.
    """
    m = MERGE_MSG_RE.search(message)
    if m:
        return m.group(1)
    sp = SPLICE_RE.match(message)
    if sp:
        return sp.group(1)

    exact = []
    candidates = []
    for bname, bhash in branches.items():
        if not is_ancestor(git_dir, second_parent, bhash):
            continue
        if first_parent and is_ancestor(git_dir, bhash, first_parent):
            continue
        candidates.append(bname)
        if bhash == second_parent:
            exact.append(bname)
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if exact:
        return exact[0]
    return candidates[0] if candidates else "unknown"


# ---------------------------------------------------------------------------
# git plumbing helpers
# ---------------------------------------------------------------------------

def git(cwd, *args, input_bytes=None):
    cmd = ["git", "-C", str(cwd)] + list(args)
    r = subprocess.run(cmd, capture_output=True, input=input_bytes)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)}: {err}")
    return r.stdout


def store_blob(git_dir: Path, data: bytes) -> str:
    return git(git_dir, "hash-object", "-w", "--stdin", input_bytes=data).decode().strip()


def make_tree(git_dir: Path, snapshot: dict) -> str:
    """Build a (possibly nested) git tree from {path: bytes}."""
    tree = {}
    for filepath, content in snapshot.items():
        parts = filepath.split("/")
        node = tree
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        blob_hash = store_blob(git_dir, content)
        node[parts[-1]] = blob_hash

    def build(node):
        lines = []
        for name in sorted(node):
            val = node[name]
            if isinstance(val, str):
                lines.append(f"100644 blob {val}\t{name}\n")
            else:
                sub = build(val)
                lines.append(f"040000 tree {sub}\t{name}\n")
        mktree_in = "".join(lines).encode()
        return git(git_dir, "mktree", input_bytes=mktree_in).decode().strip()

    if not tree:
        return git(git_dir, "mktree", input_bytes=b"").decode().strip()
    return build(tree)


def make_commit(git_dir: Path, tree: str, parents: list, msg: str) -> str:
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    args += ["-m", msg]
    return git(git_dir, *args).decode().strip()


# ---------------------------------------------------------------------------
# ant2git
# ---------------------------------------------------------------------------

def ant2git(project_dir: str, git_path: str):
    ant_dir = Path(project_dir) / ".ant"
    git_dir = Path(git_path)

    if not ant_dir.exists():
        print(f"error: {ant_dir} not found", file=sys.stderr)
        sys.exit(1)

    chambers = parse_chambers(ant_dir)
    if not chambers:
        print("no chambers found"); return

    refs = parse_refs(ant_dir)
    head = parse_head(ant_dir)

    git_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(git_dir)], check=True,
                    capture_output=True)
    print(f"initialized git repo at {git_dir}")

    commit_map = {}
    for cid in sorted(chambers):
        ch = chambers[cid]
        snap = cumulative_snapshot(chambers, cid)
        tree = make_tree(git_dir, snap)

        parents = []
        if ch.parent != 0 and ch.parent in commit_map:
            parents.append(commit_map[ch.parent])

        source = detect_splice(ch.message)
        if source and ch.parent != 0:
            theirs = find_theirs_parent(chambers, cid, ch.parent,
                                        refs.get(source))
            if theirs is not None and theirs in commit_map:
                parents.append(commit_map[theirs])

        msg = ch.message or f"chamber {cid}"
        if ch.name:
            msg = f"{msg}\n\nchamber-name: {ch.name}"

        sha = make_commit(git_dir, tree, parents, msg)
        commit_map[cid] = sha
        kind = "splice" if len(parents) > 1 else "chamber"
        label = f" ({ch.name})" if ch.name else ""
        print(f"  {kind} {cid}{label} → {sha[:10]}")

    for name, cid in refs.items():
        if cid in commit_map:
            git(git_dir, "update-ref", f"refs/heads/{name}", commit_map[cid])
            print(f"  tunnel {name} → branch {name} [{commit_map[cid][:10]}]")

    if head in refs and refs[head] in commit_map:
        git(git_dir, "symbolic-ref", "HEAD", f"refs/heads/{head}")
        git(git_dir, "checkout", head)
    elif refs:
        first = next(iter(refs))
        git(git_dir, "checkout", first)

    n = len(commit_map)
    print(f"\ndone: {n} chamber{'s'*(n!=1)} → {n} commit{'s'*(n!=1)}")


# ---------------------------------------------------------------------------
# git2ant
# ---------------------------------------------------------------------------

def git2ant(git_path: str, project_dir: str):
    git_dir = Path(git_path)
    proj = Path(project_dir)
    ant_dir = proj / ".ant"

    if not (git_dir / ".git").exists():
        print(f"error: {git_dir} is not a git repo", file=sys.stderr)
        sys.exit(1)

    raw = git(git_dir, "rev-list", "--all", "--topo-order", "--reverse").decode().strip()
    if not raw:
        print("no commits found"); return
    hashes = raw.split("\n")

    branch_raw = git(git_dir, "branch", "--format=%(refname:short) %(objectname)").decode().strip()
    branches = {}
    for line in branch_raw.split("\n"):
        if line.strip():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                branches[parts[0]] = parts[1]

    try:
        head_branch = git(git_dir, "rev-parse", "--abbrev-ref", "HEAD").decode().strip()
    except RuntimeError:
        head_branch = "main"
    if head_branch == "HEAD":
        head_branch = "main"

    ant_dir.mkdir(parents=True, exist_ok=True)
    (ant_dir / "refs").mkdir(exist_ok=True)
    (ant_dir / "chambers").mkdir(exist_ok=True)

    commit_map = {}
    next_id = 1

    for h in hashes:
        parent_line = git(git_dir, "log", "-1", "--format=%P", h).decode().strip()
        git_parents = parent_line.split() if parent_line else []
        parent_hash = git_parents[0] if git_parents else None
        parent_id = commit_map.get(parent_hash, 0)

        full_msg = git(git_dir, "log", "-1", "--format=%B", h).decode()
        message = full_msg.split("\n", 1)[0].strip()

        chamber_name = ""
        nm = CHAMBER_NAME_RE.search(full_msg)
        if nm:
            chamber_name = nm.group(1).strip()

        if len(git_parents) > 1:
            source = resolve_merge_source(git_dir, message, parent_hash,
                                          git_parents[1], branches)
            message = f"splice tunnel '{source}'"

        # build a delta chamber vs the first parent: added/modified/typechanged
        # files become content entries, deleted files become tombstones. the
        # root commit (no parent) lists its whole tree as adds. each manifest
        # entry — content or tombstone — gets a blob so line N ↔ blob N holds.
        entries = []  # list of (manifest_name, blob_bytes)
        if parent_hash is None:
            raw_tree = git(git_dir, "ls-tree", "-r", "--name-only", h).decode()
            for fpath in (f for f in raw_tree.split("\n") if f):
                entries.append((fpath, git(git_dir, "show", f"{h}:{fpath}")))
        else:
            raw_diff = git(git_dir, "diff", "--name-status", "--no-renames",
                           parent_hash, h).decode()
            for line in raw_diff.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                status, fpath = parts[0], parts[-1]
                if status.startswith("D"):
                    entries.append((BURY_MARKER + fpath, b""))
                else:
                    entries.append((fpath, git(git_dir, "show", f"{h}:{fpath}")))

        cdir = ant_dir / "chambers" / str(next_id)
        cdir.mkdir(parents=True, exist_ok=True)

        (cdir / "parent").write_text(str(parent_id))
        (cdir / "message").write_text(message)
        if chamber_name:
            (cdir / "name").write_text(chamber_name)

        for i, (_, content) in enumerate(entries):
            (cdir / str(i)).write_bytes(content)
        (cdir / "files").write_text("\n".join(name for name, _ in entries))

        commit_map[h] = next_id
        ndel = sum(1 for name, _ in entries if name.startswith(BURY_MARKER))
        tag = f" ({ndel} buried)" if ndel else ""
        print(f"  {h[:10]} → chamber {next_id}{tag}")
        next_id += 1

    for bname, bhash in branches.items():
        if bhash in commit_map:
            (ant_dir / "refs" / bname).write_text(str(commit_map[bhash]))
            print(f"  branch {bname} → tunnel {bname}")

    if not any(bhash in commit_map for bhash in branches.values()):
        last_id = commit_map[hashes[-1]]
        (ant_dir / "refs" / head_branch).write_text(str(last_id))

    (ant_dir / "HEAD").write_text(head_branch)
    (ant_dir / "next_id").write_text(str(next_id))
    (ant_dir / "forage").write_text("")

    # restore working tree from the HEAD tip's cumulative snapshot
    # (chambers are deltas now, so a single-chamber read is not enough)
    tip_ref = ant_dir / "refs" / head_branch
    if tip_ref.exists():
        tip_id = int(tip_ref.read_text().strip())
        snap = cumulative_snapshot(parse_chambers(ant_dir), tip_id)
        for fname, content in snap.items():
            out = proj / fname
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)

    n = next_id - 1
    print(f"\ndone: {len(hashes)} commit{'s'*(len(hashes)!=1)} → {n} chamber{'s'*(n!=1)}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="convert between ~ANT colonies and git repos")
    sub = p.add_subparsers(dest="cmd", required=True)

    a2g = sub.add_parser("ant2git", help=".ant/ → git")
    a2g.add_argument("project_dir", help="directory containing .ant/")
    a2g.add_argument("git_dir", help="path for new git repo")

    g2a = sub.add_parser("git2ant", help="git → .ant/")
    g2a.add_argument("git_dir", help="path to git repository")
    g2a.add_argument("project_dir", help="path for new project with .ant/")

    args = p.parse_args()
    if args.cmd == "ant2git":
        ant2git(args.project_dir, args.git_dir)
    elif args.cmd == "git2ant":
        git2ant(args.git_dir, args.project_dir)


if __name__ == "__main__":
    main()
