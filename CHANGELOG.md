# Changelog

## 0.5.0

### New

- **Backup versions.** `list`, `get`, `verify`, `info`, `doctor` and `tui` read any
  version: `--version N`, `--version @YYYY-MM-DD[ HH:MM[:SS]]` / `--before '…'` (newest
  complete version that started no later than that local time), `--allow-incomplete`.
  The default is still the newest version, but now the newest *complete* one, read for
  every share alike.
- **`hbk <archive> versions [--porcelain]`** lists versions with start time and status.
  It needs no password.
- `list` accepts the same glob as `get` when the pattern contains `*`, `?` or `[`.
- `--` ends the options.
- Each backup version gets its own cached index; superseded ones are deleted.

### Changed — check your scripts

Exit codes are now distinct and documented (README, "Exit codes"):

| situation | 0.4.8 | 0.5.0 |
|---|---|---|
| password missing (non-interactive) | 1 | 3 |
| wrong password | 1 or 2 | 3 |
| glob/pattern matched nothing (`get`, `verify`) | 1 | 5 |
| pattern matched nothing (`list`) | 0 | 5 |
| not enough disk space | 1 | 6 |
| usage error / usage text shown | 1 | 2 |
| interrupted `get`/`verify` | 0 or 1 | 130 |
| unexpected internal error | 1 | 2 |
| requested version missing / not complete | — | 4 |
| output pipe closed (`… \| head`), `list`/`versions`/`info` | 1 (traceback) | 0 |
| output pipe closed, `get`/`verify` | 1 (traceback) | 141 |

Arguments and options a command used to ignore are refused with code 2: a stray word
after the destination (an unquoted path with a space), `--strict` outside `get`, `-j` on
`list`/`info`/`doctor`, a repeated option, `--porcelain` outside `versions`.

`hbk --version` alone still prints hbkit's version; `hbk --version 3 <archive> …` now
selects backup version 3.

### Fixed

- Resuming into a directory that holds another version's restore no longer keeps a file
  just because its size matches; the restore record now names each file's source.
