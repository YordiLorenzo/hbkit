"""Restore manifest: durable, verifiable resume state for a destination directory.

Without this, resume infers completion from the destination filesystem alone: a file of
exactly the right size is assumed done. That is fine for an interrupted run of our own
(files are written to `.part` and atomically renamed, so a half-written file never appears
under its real name) but it silently accepts a file that is the right *length* and the
wrong *bytes* - which contradicts the guarantee that hbkit cannot hand you corrupt data.

So each completed file records its size and the MD5 of what was actually written, in a
JSON-lines file in the destination. Resume can then verify cheaply - hashing a local file
is far cheaper than re-extracting it - and can tell "already done" apart from "present but
wrong".

Format: one JSON object per line, appended as files finish. Append-only means an
interrupted run leaves a valid prefix; a truncated final line is skipped on read.

Concurrency: every worker process appends to the same file. Python opens "a" with
O_APPEND, and POSIX guarantees atomicity for O_APPEND writes below PIPE_BUF (4096 bytes);
a record here is ~100 bytes, so lines from parallel workers cannot interleave. Reads
tolerate a torn final line regardless.
"""
from __future__ import annotations

import hashlib
import json
import os

MANIFEST_NAME = ".hbkit-restore.jsonl"


def path_for(dest: str) -> str:
    return os.path.join(dest, MANIFEST_NAME)


def load(dest: str) -> dict[str, dict]:
    """Read the manifest. A partially-written final line is ignored, not fatal."""
    p = path_for(dest)
    out: dict[str, dict] = {}
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                      # torn tail from an interrupted write
                if "p" in rec:
                    out[rec["p"]] = rec
    except FileNotFoundError:
        pass
    return out


def append(dest: str, rel: str, size: int, md5: str) -> None:
    with open(path_for(dest), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"p": rel, "s": size, "m": md5}) + "\n")
        fh.flush()


def file_md5(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def is_complete(dest: str, rel: str, size: int, known: dict, verify: bool = False) -> bool:
    """Has `rel` already been restored correctly?

    Size is checked always. With `verify`, the recorded MD5 is checked too, which catches
    a file that is the right length but the wrong content - the one case plain size
    comparison cannot see.
    """
    target = os.path.join(dest, rel)
    try:
        if os.path.getsize(target) != size:
            return False
    except OSError:
        return False
    if not verify:
        return True
    rec = known.get(rel)
    if not rec or "m" not in rec:
        return False                              # no recorded hash: cannot vouch for it
    return file_md5(target) == rec["m"]


def sweep_parts(dest: str) -> int:
    """Delete leftover .part files from interrupted runs. Returns how many were removed."""
    n = 0
    for root, _dirs, files in os.walk(dest):
        for f in files:
            if f.endswith(".part"):
                try:
                    os.unlink(os.path.join(root, f))
                    n += 1
                except OSError:
                    pass
    return n
