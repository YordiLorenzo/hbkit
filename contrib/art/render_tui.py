"""Render hbkit's real browser screen over a demo index, as contrib/art/tui.svg.

The tree is made up (neutral share and folder names), so no archive and no personal data
is involved. Free space is pinned so the panel doesn't show this machine's disk.

    python contrib/art/render_tui.py
"""
import asyncio
import os
import pathlib
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace

from hbkit import tui

HERE = pathlib.Path(__file__).resolve().parent
G = 1 << 30

# share -> (folder, size in GiB, file count)
TREE = {
    "photo": [("2019", 48.2, 9_412), ("2020", 61.7, 12_088), ("2021", 74.3, 15_930),
              ("2022", 88.9, 19_204), ("2023", 112.6, 24_515), ("2024", 131.8, 27_871),
              ("Family scans", 9.4, 3_120), ("Phone uploads", 85.5, 42_377)],
    "video": [("Holidays", 640.2, 1_204), ("Projects", 702.9, 3_408)],
    "homes": [("alex", 58.1, 23_906), ("sam", 38.6, 17_644)],
    "music": [("Library", 48.7, 9_120)],
    "documents": [("Contracts", 1.2, 1_806), ("Scans", 8.4, 6_355), ("Taxes", 2.7, 14_643)],
}


def build_index(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE node(id INTEGER PRIMARY KEY, parent INTEGER, name TEXT, isdir "
               "INTEGER, size INTEGER, nfiles INTEGER, ovf INTEGER, mtime INTEGER, share TEXT)")
    nid = 0
    for share, folders in TREE.items():
        nid += 1
        root = nid
        db.execute("INSERT INTO node VALUES (?,NULL,?,1,?,?,NULL,0,?)",
                   (root, share, sum(int(s * G) for _, s, _ in folders),
                    sum(n for _, _, n in folders), share))
        for name, size, files in folders:
            nid += 1
            db.execute("INSERT INTO node VALUES (?,?,?,1,?,?,NULL,0,?)",
                       (nid, root, name, int(size * G), files, share))
    db.commit()
    db.close()


shutil.disk_usage = lambda p: SimpleNamespace(free=int(1.6 * (1 << 40)))


class Demo(tui.HBKApp):
    def __init__(self, index):
        super().__init__(8)
        self.index = index

    def on_mount(self, event):
        event.prevent_default()        # Textual would also run HBKApp.on_mount: the picker
        self.theme = "nord"
        self.push_screen(tui.BrowserScreen(SimpleNamespace(root="/Volumes/Backup/nas.hbk"),
                                           tui.Index(self.index)))


async def main(index):
    app = Demo(index)
    async with app.run_test(size=(118, 23)) as pilot:
        await pilot.pause()
        scr = app.screen
        scr.query_one("#dest", tui.Input).value = "/Volumes/Spare/restore"
        tree = scr.query_one("#tree", tui.Tree)
        by_name = lambda nodes, name: next(n for n in nodes if scr.idx.row(n.data[0])[0] == name)
        photo = by_name(tree.root.children, "photo")
        photo.expand()
        await pilot.pause()
        for year in ("2023", "2024"):
            tree.move_cursor(by_name(photo.children, year))
            await pilot.pause()
            scr.action_toggle()
        tree.move_cursor(by_name(photo.children, "Phone uploads"))
        await pilot.pause(0.3)
        app.save_screenshot(filename="tui.svg", path=str(HERE))


with tempfile.TemporaryDirectory() as tmp:
    build_index(os.path.join(tmp, "demo.db"))
    asyncio.run(main(os.path.join(tmp, "demo.db")))
print(HERE / "tui.svg")
