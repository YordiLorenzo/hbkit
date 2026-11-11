# Contributing to hbkit

Thanks for helping. hbkit is an open reader for Synology Hyper Backup archives, so most of the work is about reading more kinds of archive correctly.

## Reports help most

- **A restore doesn't work:** open an issue with the "A restore doesn't work" form. The output of `hbk <archive> doctor` usually shows the cause.
- **It worked:** tell us in Discussions, under [Recovered with hbkit](https://github.com/YordiLorenzo/hbkit/discussions/categories/recovered-with-hbkit). Reports from real archives are how we know what works.
- Never post an archive or private file names. The doctor output and the error are enough.

## Run the tests

```sh
git clone https://github.com/YordiLorenzo/hbkit && cd hbkit
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m pytest tests -v
```

You need liblz4 (`brew install lz4`, or `sudo apt install liblz4-1`). Most tests build small synthetic archives with `tests/synth.py`, so no real backup is needed. Tests that need a real archive skip unless `HBK_TEST_ARCHIVE` points at one.

## Changes to the format notes

`FORMAT.md` tags every claim: verified on real archives, from disassembly, inferred, or unknown. Keep the tags honest. A claim moves to verified only when it was checked against real bytes, and the PR should say how.

## Pull requests

- One change per PR, with a test when the change touches reading or restoring.
- Say what you checked, and on which kind of archive.
- Areas where help is most welcome: encrypted archives, the older record layouts, and `Pool/file_pool`.
