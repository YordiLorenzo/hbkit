#!/usr/bin/env python3
"""hbk — a TUI for recovering files from a Synology Hyper Backup archive.

Point it at any .hbk archive (or a drive containing one). It indexes on first use,
then lets you browse a collapsible tree, tick what you want, pick a destination and
watch it come out with live throughput.

    hbk-tui                         # scan for archives
    hbk-tui /Volumes/Backup         # open a specific one
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (Button, Footer, Header, Input, Label, ListItem,
                             ListView, LoadingIndicator, ProgressBar, RichLog,
                             Sparkline, Static, Tree)
from textual.widgets.tree import TreeNode


from . import archive as hbk
from . import index as hbk_index
from . import runner as hbk_run

NONE, PARTIAL, FULL = 0, 1, 2
MARK = {NONE: "☐", PARTIAL: "◪", FULL: "☑"}

FILE_ICONS = {
    **{e: "\U0001f5bc" for e in ("jpg", "jpeg", "png", "heic", "gif", "tif", "tiff", "bmp", "webp")},
    **{e: "\U0001f39e" for e in ("mov", "mp4", "m4v", "avi", "mkv", "mts", "mpg")},
    **{e: "\U0001f4f7" for e in ("rw2", "nef", "arw", "cr2", "cr3", "dng", "raf", "orf")},
    **{e: "\U0001f3b5" for e in ("mp3", "wav", "aac", "m4a", "aiff", "flac")},
    **{e: "\U0001f4c4" for e in ("pdf", "doc", "docx", "txt", "rtf", "pages")},
    **{e: "\U0001f5dc" for e in ("zip", "rar", "7z", "gz", "tar", "dmg")},
}


def human(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}B" if u == "B" else f"{n:,.1f}{u}"
        n /= 1024
    return f"{n:,.1f}T"


def hms(sec) -> str:
    if sec is None or sec != sec or sec in (float("inf"),):
        return "--:--"
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------- index access

class Index:
    """Read-only view over a pre-built node tree."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self._parent: dict[int, int | None] = {}

    def roots(self):
        return self.db.execute("SELECT id,name,isdir,size,nfiles FROM node "
                               "WHERE parent IS NULL ORDER BY size DESC").fetchall()

    def children(self, nid):
        return self.db.execute("SELECT id,name,isdir,size,nfiles FROM node WHERE parent=? "
                               "ORDER BY isdir DESC, name COLLATE NOCASE", (nid,)).fetchall()

    def row(self, nid):
        return self.db.execute("SELECT name,isdir,size,nfiles FROM node WHERE id=?",
                               (nid,)).fetchone()

    def parent(self, nid):
        if nid not in self._parent:
            r = self.db.execute("SELECT parent FROM node WHERE id=?", (nid,)).fetchone()
            self._parent[nid] = r[0] if r else None
        return self._parent[nid]

    def ancestors(self, nid):
        out, p = [], self.parent(nid)
        while p is not None:
            out.append(p)
            p = self.parent(p)
        return out

    def size(self, nid):
        r = self.db.execute("SELECT size FROM node WHERE id=?", (nid,)).fetchone()
        return (r[0] or 0) if r else 0

    def nfiles(self, nid):
        r = self.db.execute("SELECT nfiles FROM node WHERE id=?", (nid,)).fetchone()
        return (r[0] or 0) if r else 0

    def totals(self):
        r = self.db.execute("SELECT SUM(nfiles), SUM(size) FROM node "
                            "WHERE parent IS NULL").fetchone()
        return (r[0] or 0), (r[1] or 0)

    def subtree_files(self, nid):
        """Every file under nid, keyed by its FULL archive path (share name first), so a
        file always lands in the same place regardless of which folder you ticked - which
        is what makes resume work, and matches the CLI."""
        prefix = "".join("/" + self.row(a)[0] for a in reversed(self.ancestors(nid)))
        return self.db.execute("""
            WITH RECURSIVE sub(id,path,isdir) AS (
                SELECT id, ?||'/'||name, isdir FROM node WHERE id=?
                UNION ALL
                SELECT n.id, sub.path||'/'||n.name, n.isdir
                  FROM node n JOIN sub ON n.parent=sub.id
            )
            SELECT sub.path, n.ovf, n.size, n.mtime
              FROM sub JOIN node n ON n.id=sub.id
             WHERE sub.isdir=0 AND n.ovf IS NOT NULL
        """, (prefix, nid)).fetchall()

    def search(self, term, limit=1):
        return self.db.execute("SELECT id FROM node WHERE name LIKE ? "
                               "ORDER BY size DESC LIMIT ?", (f"%{term}%", limit)).fetchall()


# ------------------------------------------------------------------ selection

@dataclass
class Selection:
    """Explicitly ticked node ids; a ticked directory implies its whole subtree."""
    idx: Index
    chosen: set[int] = field(default_factory=set)
    marks: Counter = field(default_factory=Counter)

    def state(self, nid) -> int:
        if nid in self.chosen:
            return FULL
        for a in self.idx.ancestors(nid):
            if a in self.chosen:
                return FULL
        return PARTIAL if self.marks[nid] else NONE

    def toggle(self, nid):
        if self.state(nid) == FULL:
            if nid in self.chosen:
                self._remove(nid)
            else:
                for a in self.idx.ancestors(nid):
                    if a in self.chosen:
                        self._untick_within(a, nid)
                        break
        else:
            for c in [c for c in self.chosen if nid in self.idx.ancestors(c)]:
                self._remove(c)                    # subsumed by this tick
            self._add(nid)

    def _add(self, nid):
        self.chosen.add(nid)
        for a in self.idx.ancestors(nid):
            self.marks[a] += 1

    def _remove(self, nid):
        self.chosen.discard(nid)
        for a in self.idx.ancestors(nid):
            self.marks[a] -= 1
            if self.marks[a] <= 0:
                del self.marks[a]

    def _untick_within(self, ancestor, exclude):
        """Drop one node out of a ticked ancestor by ticking its siblings instead."""
        self._remove(ancestor)
        chain = [exclude] + self.idx.ancestors(exclude)
        chain = chain[:chain.index(ancestor) + 1]
        for i, node in enumerate(chain[:-1]):
            for cid, *_ in self.idx.children(chain[i + 1]):
                if cid != node:
                    self._add(cid)

    def totals(self):
        return (len(self.chosen),
                sum(self.idx.size(n) for n in self.chosen),
                sum(self.idx.nfiles(n) for n in self.chosen))

    def files(self):
        out = []
        for n in self.chosen:
            out.extend(self.idx.subtree_files(n))
        return out


# ------------------------------------------------------------- archive picker

def scan_for_archives(roots=None) -> list[str]:
    found = []
    for base in roots or ["/Volumes", os.path.expanduser("~")]:
        if not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for e in entries:
            if e.startswith("."):
                continue
            p = os.path.join(base, e)
            if not os.path.isdir(p):
                continue
            try:
                if hbk.is_archive(p):
                    found.append(p)
                    continue
                for sub in os.listdir(p):
                    if sub.startswith("."):
                        continue
                    q = os.path.join(p, sub)
                    if os.path.isdir(q) and hbk.is_archive(q):
                        found.append(q)
            except OSError:
                continue
    return sorted(set(found))


class ArchiveScreen(Screen):
    BINDINGS = [Binding("enter", "open", "Open"), Binding("q", "quit", "Quit")]

    def __init__(self, initial: str | None = None):
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="picker"):
            yield Label("Hyper Backup Recovery", id="title")
            yield Label("Point at a .hbk archive, or a drive containing one.", classes="dim")
            yield Input(value=self.initial or "", placeholder="/Volumes/… ", id="path")
            yield Static("", id="probe")
            yield Label("Detected archives", classes="sect")
            yield ListView(id="found")
            yield Button("Open archive", variant="success", id="open")
        yield Footer()

    def on_mount(self):
        self.query_one("#path", Input).focus()
        if self.initial:
            self.probe(self.initial)
        self.discover()

    @work(thread=True)
    def discover(self):
        for p in scan_for_archives():
            self.app.call_from_thread(self.add_found, p)

    def add_found(self, path: str):
        lv = self.query_one("#found", ListView)
        lv.append(ListItem(Label(path), name=path))
        if not self.query_one("#path", Input).value:
            self.query_one("#path", Input).value = path
            self.probe(path)

    @on(ListView.Selected, "#found")
    def pick(self, ev: ListView.Selected):
        self.query_one("#path", Input).value = ev.item.name or ""
        self.probe(ev.item.name or "")

    @on(Input.Changed, "#path")
    def path_changed(self, ev: Input.Changed):
        self.probe(ev.value)

    def probe(self, path: str):
        w = self.query_one("#probe", Static)
        if not path.strip():
            w.update(Text("enter a path", style="dim"))
            return
        try:
            arc = hbk.Archive(path)
        except Exception as e:                              # noqa: BLE001
            w.update(Text(str(e), style="red"))
            self._arc = None
            return
        self._arc = arc
        cfg = arc.task_config()
        lines = Text()
        lines.append("✓ ", style="bold green")
        lines.append(arc.root + "\n", style="bold")
        if cfg.get("name"):
            lines.append(f"  task    {cfg['name']}   host {cfg.get('host_name','?')}\n", style="dim")
        lines.append(f"  shares  {', '.join(arc.shares()) or '(none)'}\n", style="dim")
        comp = {"0": "none", "1": "lz4", "2": "lz4-hc", "4": "zlib"}.get(
            cfg.get("data_compress_type", ""), cfg.get("data_compress_type", "?"))
        lines.append(f"  codec   {comp}", style="dim")
        if arc.is_encrypted():
            lines.append("\n  ⚠ ENCRYPTED — extraction not supported", style="bold red")
        else:
            lines.append("   unencrypted\n", style="dim")
        indexed = hbk_index.is_current(arc)
        lines.append("  index   " + ("ready" if indexed else "will be built on open"),
                     style="green" if indexed else "yellow")
        w.update(lines)

    @on(Button.Pressed, "#open")
    def open_pressed(self):
        self.action_open()

    def action_open(self):
        arc = getattr(self, "_arc", None)
        if arc is None:
            self.notify("no valid archive at that path", severity="error")
            return
        if arc.is_encrypted():
            self.notify("archive is encrypted — cannot extract", severity="error", timeout=8)
            return
        self.app.push_screen(IndexScreen(arc))


class IndexScreen(Screen):
    """Builds the browse index if it is missing or stale, then opens the browser."""

    def __init__(self, arc: hbk.Archive):
        super().__init__()
        self.arc = arc

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="indexing"):
            yield Label("Indexing archive", id="title")
            yield Label(self.arc.root, classes="dim")
            yield LoadingIndicator()
            yield Static("starting…", id="stage")
            yield Static("", id="hint")
        yield Footer()

    def on_mount(self):
        self.query_one("#hint", Static).update(
            Text("first open only — the index is cached and reused", style="dim"))
        self.build()

    @work(thread=True, exclusive=True)
    def build(self):
        def prog(stage, done, total):
            self.app.call_from_thread(
                self.query_one("#stage", Static).update,
                Text.assemble((stage, "bold"), (f"   [{done}/{total}]", "dim")))
        try:
            path = hbk_index.open_or_build(self.arc, progress=prog)
        except Exception as e:                              # noqa: BLE001
            self.app.call_from_thread(self.failed, f"{type(e).__name__}: {e}")
            return
        self.app.call_from_thread(self.done, path)

    def failed(self, msg):
        self.query_one("#stage", Static).update(Text(msg, style="bold red"))
        self.notify(msg, severity="error", timeout=10)

    def done(self, path):
        self.app.switch_screen(BrowserScreen(self.arc, Index(path)))


# -------------------------------------------------------------- browser screen

class BrowserScreen(Screen):
    BINDINGS = [
        # priority: the focused Tree binds space itself and would otherwise swallow it
        Binding("space", "toggle", "Select", priority=True),
        Binding("a", "select_all", "All", priority=True),
        Binding("n", "select_none", "Clear", priority=True),
        Binding("d", "focus_dest", "Destination"),
        Binding("r", "run", "Recover"),
        Binding("slash", "focus_search", "Search"),
        Binding("escape", "app.pop_screen", "Archives"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, arc: hbk.Archive, idx: Index):
        super().__init__()
        self.arc = arc
        self.idx = idx
        self.sel = Selection(idx)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(placeholder="search filename…  (/)", id="search")
                yield Tree("archive", id="tree")
            with VerticalScroll(id="right"):
                yield Label("Selection", classes="sect")
                yield Static("nothing selected", id="sel-info")
                yield Label("Destination", classes="sect")
                yield Input(value=os.path.expanduser("~/Pictures/hbk"), id="dest")
                yield Static("", id="space-info")
                yield Button("Start recovery", variant="success", id="go")
                yield Label("Archive", classes="sect")
                yield Static("", id="arch-info")
        yield Footer()

    def on_mount(self):
        tree = self.query_one("#tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        for nid, name, isdir, size, nf in self.idx.roots():
            node = tree.root.add(self._label(nid, name, isdir, size, nf),
                                 allow_expand=bool(isdir))
            node.data = (nid, isdir)
        tree.focus()
        files, total = self.idx.totals()
        self.query_one("#arch-info", Static).update(
            Text.assemble((f"{files:,}", "bold"), (" files  ", "dim"), (human(total), "bold"),
                          ("\n", ""), (self.arc.root, "dim")))
        self.update_sel()

    def _label(self, nid, name, isdir, size, nf) -> Text:
        st = self.sel.state(nid)
        colour = {NONE: "dim", PARTIAL: "yellow", FULL: "bold green"}[st]
        if isdir:
            icon, style = "\U0001f4c1", "bold"
        else:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            icon, style = FILE_ICONS.get(ext, "\U0001f4c4"), ""
        shown = name if len(name) <= 44 else name[:43] + "\u2026"   # keep the size column aligned
        t = Text.assemble((MARK[st] + " ", colour), (icon + " ", ""), (shown, style))
        t.append(" " * max(1, 46 - len(shown)))
        t.append(human(size).rjust(8), style="cyan" if isdir else "dim")
        if isdir and nf:
            t.append(f"{nf:,}".rjust(10), style="dim")
        return t

    def populate(self, node: TreeNode):
        """Lazily fill a node's children from the index. Idempotent."""
        if node.children or node.data is None:
            return
        for cid, name, cisdir, size, nf in self.idx.children(node.data[0]):
            child = node.add(self._label(cid, name, cisdir, size, nf), allow_expand=bool(cisdir))
            child.data = (cid, cisdir)

    @on(Tree.NodeExpanded)
    def load_children(self, ev: Tree.NodeExpanded):
        self.populate(ev.node)

    def relabel(self, node: TreeNode):
        for child in node.children:
            if child.data:
                nid = child.data[0]
                row = self.idx.row(nid)
                if row:
                    child.set_label(self._label(nid, *row))
                self.relabel(child)

    def action_toggle(self):
        tree = self.query_one("#tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        self.sel.toggle(node.data[0])
        self.relabel(tree.root)
        self.update_sel()

    def action_select_all(self):
        self.sel.chosen.clear()
        self.sel.marks.clear()
        for nid, *_ in self.idx.roots():
            self.sel._add(nid)
        self.relabel(self.query_one("#tree", Tree).root)
        self.update_sel()

    def action_select_none(self):
        self.sel.chosen.clear()
        self.sel.marks.clear()
        self.relabel(self.query_one("#tree", Tree).root)
        self.update_sel()

    def update_sel(self):
        n, size, files = self.sel.totals()
        w = self.query_one("#sel-info", Static)
        if not n:
            w.update(Text("nothing selected", style="dim"))
        else:
            w.update(Text.assemble((f"{files:,}", "bold"), (" files\n", "dim"),
                                   (human(size), "bold green"), ("  in ", "dim"),
                                   (f"{n}", "bold"), (" item(s)", "dim")))
        self.update_space()

    def update_space(self):
        probe = self.query_one("#dest", Input).value
        while probe and not os.path.exists(probe):
            probe = os.path.dirname(probe)
        w = self.query_one("#space-info", Static)
        if not probe:
            w.update(Text("invalid path", style="bold red"))
            return
        free = shutil.disk_usage(probe).free
        _, need, _ = self.sel.totals()
        if need > free:
            w.update(Text.assemble(("⚠ needs ", "bold red"), (human(need), "bold red"),
                                   (", only ", "red"), (human(free), "bold red"), (" free", "red")))
        else:
            w.update(Text.assemble(("✓ ", "green"), (human(free), "bold"), (" free", "dim")))

    @on(Input.Changed, "#dest")
    def dest_changed(self):
        self.update_space()

    @on(Input.Submitted, "#search")
    def do_search(self, ev: Input.Submitted):
        term = ev.value.strip()
        if not term:
            return
        hits = self.idx.search(term, 1)
        if not hits:
            self.notify(f"no match for {term!r}", severity="warning")
            return
        if not self.reveal(hits[0][0]):
            self.notify("found it, but could not expand the path", severity="warning")
            return
        self.query_one("#tree", Tree).focus()

    def reveal(self, nid: int) -> bool:
        """Expand down to `nid` and park the cursor on it."""
        tree = self.query_one("#tree", Tree)
        chain = list(reversed(self.idx.ancestors(nid))) + [nid]
        node = tree.root
        for want in chain:
            self.populate(node)
            node.expand()
            match = next((c for c in node.children if c.data and c.data[0] == want), None)
            if match is None:
                return False
            node = match
        target = node

        def place():                    # the tree needs a refresh to assign line numbers
            tree.move_cursor(target)    # before move_cursor can find the node
            tree.scroll_to_node(target)

        self.call_after_refresh(place)
        return True

    def action_focus_dest(self):
        self.query_one("#dest", Input).focus()

    def action_focus_search(self):
        self.query_one("#search", Input).focus()

    @on(Button.Pressed, "#go")
    def go(self):
        self.action_run()

    def action_run(self):
        if not self.sel.chosen:
            self.notify("select something first — space to tick", severity="warning")
            return
        dest = os.path.abspath(os.path.expanduser(self.query_one("#dest", Input).value))
        files = self.sel.files()
        if not files:
            self.notify("selection contains no extractable files", severity="error")
            return
        try:
            os.makedirs(dest, exist_ok=True)
        except OSError as e:
            self.notify(f"cannot create {dest}: {e}", severity="error", timeout=8)
            return
        need = sum(f[2] for f in files)
        free = shutil.disk_usage(dest).free
        if need > free:
            self.notify(f"not enough space: need {human(need)}, have {human(free)}",
                        severity="error", timeout=10)
            return
        self.app.push_screen(ProgressScreen(self.arc.root, files, dest, self.app.n_workers))


# ------------------------------------------------------------- progress screen

class ProgressScreen(Screen):
    BINDINGS = [Binding("escape,q", "back", "Back"), Binding("c", "cancel", "Cancel")]

    def __init__(self, root, files, dest, n_workers):
        super().__init__()
        self.dest = dest
        self.runner = hbk_run.Runner(root, dest, files, n_workers)
        self.t0 = time.time()
        self.hist = [0.0] * 60
        self._last = (self.t0, 0)
        self._logged_errors = 0
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="prog"):
            yield Label(f"Recovering \u2192 {self.dest}", id="dest-line")
            yield ProgressBar(total=max(self.runner.total_bytes, 1), id="bar", show_eta=False)
            with Horizontal(id="stats"):
                yield Static(id="s-files")
                yield Static(id="s-bytes")
                yield Static(id="s-speed")
                yield Static(id="s-eta")
            yield Sparkline(self.hist, id="spark")
            yield Label("Activity", classes="sect")
            yield RichLog(id="log", highlight=False, markup=True, max_lines=500)
        yield Footer()

    def on_mount(self):
        r = self.runner
        self.query_one("#log", RichLog).write(
            f"[dim]{r.total_files:,} files \u00b7 {human(r.total_bytes)} \u00b7 "
            f"{r.n_workers} workers[/dim]")
        r.start()
        self.refresh_stats()
        self.set_interval(0.25, self.tick)

    def tick(self):
        if self._finished:
            return
        r = self.runner
        r.poll()
        log = self.query_one("#log", RichLog)
        while self._logged_errors < len(r.errors):
            path, msg = r.errors[self._logged_errors]
            self._logged_errors += 1
            log.write(f"[red]FAIL[/red] {path}  [dim]{msg}[/dim]")
        self.refresh_stats()
        if r.finished:
            self.finish()

    def refresh_stats(self):
        r = self.runner
        now = time.time()
        dt = now - self._last[0]
        if dt >= 0.5:
            self.hist = (self.hist + [(r.bytes_done - self._last[1]) / dt / 1e6])[-60:]
            self.query_one("#spark", Sparkline).data = self.hist
            self._last = (now, r.bytes_done)
        el = max(now - self.t0, 1e-6)
        avg = r.bytes_done / el
        left = (r.total_bytes - r.bytes_done) / avg if avg > 0 else None
        self.query_one("#bar", ProgressBar).update(progress=r.bytes_done)
        self.query_one("#s-files", Static).update(
            Text.assemble(("files  ", "dim"), (f"{r.done_files:,}/{r.total_files:,}", "bold")))
        self.query_one("#s-bytes", Static).update(
            Text.assemble(("data  ", "dim"),
                          (f"{human(r.bytes_done)}/{human(r.total_bytes)}", "bold")))
        self.query_one("#s-speed", Static).update(
            Text.assemble(("speed  ", "dim"), (f"{avg/1e6:,.0f} MB/s", "bold green")))
        self.query_one("#s-eta", Static).update(
            Text.assemble(("eta  ", "dim"), (hms(left), "bold")))

    def finish(self):
        self._finished = True
        r = self.runner
        r.cleanup()
        el = max(time.time() - self.t0, 1e-6)
        log = self.query_one("#log", RichLog)
        log.write("")
        verb = "Cancelled" if r.cancelled else "Finished"
        log.write(f"[b green]{verb}[/b green] in {hms(el)} \u2014 "
                  f"{r.ok:,} extracted, {r.skipped:,} already present, "
                  f"[{'red' if r.failed else 'green'}]{r.failed:,} failed[/]")
        log.write(f"[dim]average {r.bytes_done/el/1e6:,.0f} MB/s \u00b7 Esc to go back[/dim]")
        self.query_one("#dest-line", Label).update(f"{verb} \u2192 {self.dest}")

    def action_cancel(self):
        if not self._finished:
            self.runner.cancel()
            self.query_one("#log", RichLog).write("[yellow]cancelling\u2026[/yellow]")

    def action_back(self):
        self.runner.cancel()
        self.runner.cleanup()
        self.dismiss()


# ---------------------------------------------------------------------- app

class HBKApp(App):
    CSS = """
    Screen { background: $surface; }
    .dim { color: $text-muted; }
    .sect { color: $accent; text-style: bold; margin: 1 0 0 0; }
    #title { text-style: bold; color: $accent; }

    #picker { padding: 1 3; height: 1fr; }
    #picker Input { margin: 1 0; }
    #probe { padding: 1 0; min-height: 6; }
    #found { height: 1fr; border: tall $primary-darken-3; }
    #open { width: 100%; margin-top: 1; }

    #indexing { padding: 2 3; height: 1fr; align: center middle; }
    #indexing LoadingIndicator { height: 3; }

    #body { height: 1fr; }
    #left  { width: 3fr; border-right: solid $primary-darken-2; }
    #right { width: 1fr; padding: 0 1; min-width: 36; }
    #tree  { height: 1fr; padding: 0 1; }
    #search { margin: 0 1; }
    #sel-info, #space-info, #arch-info { padding: 0 0 1 0; }
    #go { width: 100%; margin: 1 0; }

    #prog { padding: 1 2; height: 1fr; }
    #bar { width: 100%; margin: 1 0; }
    #stats { height: 3; }
    #stats Static { width: 1fr; content-align: left middle; }
    #spark { height: 4; margin: 1 0; color: $success; }
    #log { height: 1fr; border: tall $primary-darken-3; padding: 0 1; }
    #dest-line { margin-bottom: 1; text-style: bold; }
    """
    TITLE = "Hyper Backup Recovery"

    def __init__(self, n_workers: int, initial: str | None = None):
        super().__init__()
        self.n_workers = n_workers
        self.initial = initial

    def on_mount(self):
        self.theme = "nord"
        self.push_screen(ArchiveScreen(self.initial))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="TUI recovery for Synology Hyper Backup archives")
    ap.add_argument("archive", nargs="?", help="path to a .hbk archive or a drive holding one")
    ap.add_argument("-j", "--workers", type=int, default=8, help="parallel workers (default 8)")
    a = ap.parse_args()
    HBKApp(a.workers, a.archive).run()


if __name__ == "__main__":
    main()
