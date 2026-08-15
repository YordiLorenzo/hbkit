

# hbkit

[![PyPI](https://img.shields.io/pypi/v/hbkit)](https://pypi.org/project/hbkit/)
[![CI](https://github.com/YordiLorenzo/hbkit/actions/workflows/ci.yml/badge.svg)](https://github.com/YordiLorenzo/hbkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Recover files from **Synology Hyper Backup (`.hbk`)** archives without any Synology software.

Point it at a backup on a local disk, an external drive, or a network mount, browse it as
a tree, and pull out what you want. Works headless on Linux and macOS, including Apple Silicon, where Synology's
own Hyper Backup Explorer is awkward or unavailable.

```sh
brew install YordiLorenzo/tap/hbkit     # macOS / Linux, pulls in liblz4 for you

hbk /Volumes/Backup doctor              # can this archive be recovered?
hbk /Volumes/Backup doctor -p secret    # encrypted? add a password
hbk-tui /Volumes/Backup                 # browse and select interactively
hbk /Volumes/Backup get "/Photos/*" ~/restore
```

### Other ways to install

```sh
pip install hbkit          # any platform; also needs liblz4 (see below)
pipx install hbkit         # same, kept in its own environment
```

Arch users can build from [`packaging/aur/PKGBUILD`](packaging/aur/PKGBUILD); a nixpkgs
derivation lives in [`packaging/nix/package.nix`](packaging/nix/package.nix).

`liblz4` is a runtime requirement — chunks are raw LZ4 blocks, loaded with `dlopen`, so a
missing library only shows up when you extract something. The Homebrew formula installs it
for you. Otherwise: `brew install lz4`, `sudo apt install liblz4-1`, or
`sudo pacman -S lz4`. Set `HBK_LZ4` if yours lives somewhere unusual. Note: Windows is not verified for extraction; the package imports and runs, but chunk extraction has not been tested on that platform.

---

## Why

Hyper Backup Explorer is a GUI, has no command line, ships x86-only on Linux, and gets
unhappy with large archives. If your NAS died and the backup is all you have, you want
something you can point at a drive, script, and trust.

`hbkit` reads the format directly. Every chunk it returns has been checked against the
archive's own MD5 and CRC32, so **it cannot silently hand you corrupt data** — the worst
case is a loud failure naming the file.

## The TUI

```
⭘                          Hyper Backup Recovery                          17:11:17
 ┌──────────────────────────────────────────────────────────────┐ │
 │  search filename…  (/)                                       │ │ Selection
 └──────────────────────────────────────────────────────────────┘ │ 197,607 files
 ▼ ◪ 📁 NAS Volume 1                       4.4T   499,745         │ 625.0G  in 1 item(s)
 ├─ ▶ ☐ 📁 Archive 2022                      24.2G       358      │
 ├─ ▶ ☐ 📁 Backups                           23.5G    63,693      │ Destination
 ├─ ▶ ☐ 📁 Video Projects                     1.9T    64,473      │ ┌──────────────────────────┐
 ├─ ▶ ☐ 📁 Media Library                      1.3T    66,629      │ │ ~/restore                │
 ├─ ▼ ☑ 📁 Photo Libraries                  625.0G   197,607      │ └──────────────────────────┘
 │  ├─ ▶ ☑ 📁 Photos Library - Laptop…        28.8G    76,844     │ ⚠ needs 625.0G, only 70.3G free
 │  ├─ ▶ ☑ 📁 Photos Library - Old Backup…    19.8G    15,505     │
                                                                  │      Start recovery
 a All  n Clear  d Destination  r Recover  / Search  q Quit       │
```

`space` tick · `a` all · `n` clear · `/` search · `d` destination · `r` recover ·
`ctrl+q` quit (plain `q` types into whichever field has focus, so quit is ctrl+q).

During a restore it shows live throughput, ETA, a rolling list of recently completed files,
and a log of any failures.

Folders show subtree size and file count. Ticking a folder takes its whole subtree; the
destination panel warns before you start if the selection will not fit. Recovery shows a
live progress bar, throughput, ETA and a failure log.

## Commands

```sh
hbk <archive> doctor                            # probe an archive, prove it's readable
hbk <archive> info                              # task name, codec, shares, encryption
hbk <archive> list [pattern]                    # search the file index
hbk <archive> get <glob> <dest> [-j N]          # extract, preserving tree and mtimes
hbk <archive> get <glob> <dest> --strict        # ...re-verifying checksums on resume
hbk <archive> verify <glob> [-j N]              # integrity-check, write nothing
hbk <archive> tui                               # same as hbk-tui

hbk mount <remote:bucket> <dir> [--for browse|restore]   # mount object storage
hbk warm <dir>                                  # pre-cache directory listings
hbk unmount <dir>
hbk remotes                                     # list configured rclone remotes
```

For encrypted archives pass `-p/--password`, set `HBK_PASSWORD`, or let it prompt. The TUI
shows a password field when it detects encryption.

`<archive>` is a `.hbk` directory, or any drive or folder containing one — it will find it.
Globs match the full archive path, which begins with the share name.

**Start with `doctor`.** It reports the layout it found and then *proves* the archive is
readable by rebuilding a random sample of real files with full checksum verification:

```
              archive : /Volumes/Backup/nas_1.hbk
                 task : Daily Backup
          source host : nas
         source model : DS...
          chunk codec : lz4
  virtual_file record : 56 B
   chunk_index record : 29 B (v3)
  bucket index record : 32 B (md5+crc32)
               shares : Photos, Documents

  PASS  virtual_file layout known  (56 B)
  PASS  chunk_index layout known  (29 B)
  PASS  bucket layout known  (32 B)
  PASS  rebuilt 9 sampled files, all chunks verified  (9 ok, 0 failed)

  VERDICT: recoverable. Sampled files rebuilt byte-exact and checksum-verified.
```

## Behaviour worth knowing

- **Resumable.** Correctly-sized files are skipped, so re-running a big job is cheap.
- **Crash-safe.** Files are written to `.part` and atomically renamed, so an interrupted
  run never leaves a truncated file that a later resume would trust.
- **Layout preserved.** Output goes to `<dest>/<share>/<original path>` with original mtimes.
- **Read-only.** Nothing is ever written to the archive.
- **Sidecars skipped.** `@eaDir`, `@SynoEAStream` and `@SynoResource` are Synology
  metadata — thumbnails and xattr streams, not your data. In one real archive they were
  half of all entries but under 1% of the bytes.
- **Index cached** per archive in `~/.cache/hbkit` (override with `HBK_CACHE`), rebuilt
  automatically when the archive changes. Browsing 1.1M files is instant after the first
  open; building it takes ~10s locally, ~48s over a network mount.

## Network mounts (rclone / S3 / R2)

You can point hbkit at a bucket mounted with rclone instead of copying the archive down
first. Opening an archive no longer measures every index shard — shards are a fixed 8 MiB
except the last, so offsets are computed: one directory listing and a single stat per index
family, and `file_chunk<N>.index` families open only if a file references them. On a 3 TB
archive that removed roughly **2,600 network round-trips** from startup.

### Setting up the mount

hbkit can drive `rclone` for you. It stores no credentials and talks to no cloud API —
`rclone config` still owns all of that — it just picks flags that are easy to get wrong.

```sh
rclone config                                   # add an s3 remote; for R2 pick "Cloudflare"
hbk remotes                                     # what's configured
hbk mount r2:mybucket ~/mnt/backup --for browse # mount + pre-warm
hbk ~/mnt/backup/target.hbk doctor
hbk unmount ~/mnt/backup
```

`--for browse` (default) uses range requests; `--for restore` uses whole-file fetching as
read-ahead with the cache capped at 50G (`--cache-size 10G` to change it). `--no-warm`
skips pre-warming, `--dry-run` just prints the rclone command.

Measured on a 3 TB archive in Cloudflare R2:

| | before tuning | with `hbk mount` |
|---|---|---|
| cold mount + directory pre-warm | ~12 min | **16 s** |
| opening the archive | 9 min 21 s | **1.8 s** |
| extracting a 10 KB file | 584 s | **11 s** |

Three things get you there, and you can apply them by hand if you'd rather not use
`hbk mount`:

- **`--no-modtime`** — the big one. Without it rclone issues a HEAD per object just to fill
  in modification times for a listing: ~0.27 s per entry, so the 2,021-shard `chunk_index`
  directory took **546.8 s**. With it, **4.07 s** — 134× faster. hbkit never uses the
  modtimes of an archive's internal files (restored mtimes come from the archive's own
  metadata), so this costs nothing.
- **`--dir-cache-time 72h`** — the first listing of a large shard directory is the expensive
  one; cache it and every later open is instant.
- **pre-warming** — `hbk mount` walks the index directories once up front, so the wait
  happens visibly at mount time instead of looking like a hang on your first command. Run
  it separately with `hbk warm ~/mnt/backup`.

### Choosing `--vfs-cache-mode` — it depends on what you're doing

`hbk mount --for` picks this for you, but if you mount by hand: rclone's `full` mode
downloads **whole files** while `off` issues **range requests**.

| What you're doing | Mode | Why |
|---|---|---|
| First index build | `full` | Reads one share database end to end (356 MB on a 1.1M-file archive). Sequential — what whole-file fetching is good at. One time; cached in `~/.cache/hbkit` after. |
| Browsing, `list`, the TUI tree | either | Served from the local index, no network at all. |
| Pulling out a few files | `off` | hbkit reads a 32-byte index record and a ~5 KB chunk at a time. In `full` mode each pulls a whole file: a measured 10 KB extraction fetched ~82 MB and took ten minutes. |
| Bulk restoring a folder | `full` | You touch most of each ~50 MB bucket anyway, so whole-file fetching becomes read-ahead. **Cap it** with `--vfs-cache-max-size`, or it will fill your disk. |

### Expectations and tuning

Profiled against a 3 TB archive in R2 (150 MB, 18,104 chunks):

| phase | share of wall time |
|---|---:|
| reading chunk data | 53% |
| reading bucket indexes | 20% |
| chunk_index lookups | 15% |
| **LZ4 + MD5 + CRC32** | **0.7%** |

Two things follow. **Verification is free over a network** — never turn it off to go faster,
it buys nothing. And throughput is bound by round-trips, so **concurrency is the only knob
that matters**: measured 4.0 MB/s at `-j 1`, 8.5 at `-j 8`, 9.5 at `-j 16`, then flat.
**Use `-j 16` on a network mount**; the default of 8 is tuned for local disks.

Things that were measured and did *not* help, so you needn't try them: enlarging the read
buffers (latency-bound, no effect), and reading only the 32 bytes needed from a bucket
index instead of caching the whole 200 KB file (18% *slower* — a file's chunks cluster in
one bucket, so the whole-index read amortises). Read amplification is 0.82×: compression
and dedup mean hbkit fetches less than it delivers.



Even configured well, a mounted bucket is dramatically slower than local storage — the
access pattern is thousands of small scattered reads, and each one crosses the network.
**If you can, copy the archive to a local disk first.** Start with one small folder and
watch the throughput before committing to a large restore.

## Performance

Use `-j` to set worker processes (default 8). Threads do not help — extraction is
GIL-bound in Python, measured flat at ~32 MB/s from 1 to 12 threads — so `hbkit` fans out
to real processes.

Throughput is bounded by the source device, not by `hbkit`. On a USB spinning disk with a
92 MB/s sequential ceiling, a cold parallel run reached 58 MB/s while the disk itself sat
at 49 MB/s; scattered reads across tens of thousands of bucket files never reach sequential
speed. Work is ordered by locality so each worker sweeps the pool in one direction rather
than several heads chasing several regions.

Media does not compress — measured ratio 1.004 on video. The space saving in a Hyper Backup
archive comes from cross-file dedup, not per-file compression, so expect bytes-off-disk to
roughly equal bytes-delivered.

## Scope and limits

Read this before trusting it with the only copy of anything.

- **Encrypted archives are supported** (password only). A wrong password is rejected
  instantly, before any data is read, by deriving the public key and comparing it to the
  one stored in the archive. Older RSA-wrapped archives and key-file unlock are *not*
  implemented — only the X25519/Argon2 scheme.
- **The index cache stores decrypted filenames.** Browsing an encrypted archive requires
  the password because the directory tree itself is ciphertext, so `~/.cache/hbkit` will
  contain plaintext names (not file contents). Delete it if that matters to you.
- **Proven against a limited set of archives.** A 3 TB DSM 7 / Hyper Backup 4.1.2
  archive (unencrypted, LZ4, single version, single pool) and a small encrypted one from
  the same DSM version, both verified byte-exact. Older record layouts
  (16-byte `chunk_index`, 28-byte bucket records), zlib chunks and multi-version archives
  are implemented from disassembly but have not met a real archive of that kind. `doctor`
  exists precisely so you can find out in seconds rather than mid-restore.
- **Unknown layouts are refused, not guessed.** A wrong guess would mean silently wrong
  bytes, which is the one thing a recovery tool must never do.
- **Whole-file dedup** (`off_virtual_file = -1`, files living in `Pool/file_pool`) is not
  decoded. One file in 501,278 in the reference archive.

## The format

[`FORMAT.md`](FORMAT.md) is a full specification of the on-disk format, written so you can
implement a reader in any language. Every claim is tagged **verified / from disassembly /
inferred / unknown**, and there is an explicit list of what is still undecoded.

As far as we can tell no public description of this format existed before it — searching
the container magic `70 53 A8 6E` returns nothing on the open web or in file-signature
databases. If the tool is useless to you, the spec may not be.

It was derived two ways and cross-checked: empirically, by anchoring on a file whose bytes
could be recognised and then rebuilding progressively larger files until a 674 MB video
reproduced exactly across 82,313 chunks; and by reading exported C++ symbols in Synology's
own `HyperBackupExplorer` binary, which ships with full symbols and gives exact field
offsets. Where the two disagreed, the empirical result won.

## Prior art

- [TeamDman/teamy-hyper-backup-explorer](https://github.com/TeamDman/teamy-hyper-backup-explorer) — independent Rust implementation (MPL-2.0). Its constants agree with what we derived separately.
- [mistersandman/hyperbackup_decrypt](https://github.com/mistersandman/hyperbackup_decrypt) — 2016 Python 2 script, and the only public reference for the **encrypted** variant.

## Development

```sh
git clone https://github.com/YordiLorenzo/hbkit && cd hbkit
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
HBK_TEST_ARCHIVE=/path/to/backup ./.venv/bin/python -m pytest tests -v
```

The test suite needs a real archive — correctness is checked against the archive's own
checksums and against file-format markers, so a pass means the bytes are genuinely right,
not merely the right length. Tests skip cleanly when no archive is available.

Contributions especially welcome for: encrypted archives, the legacy record layouts, and
`Pool/file_pool`. If you have an archive `doctor` cannot read, an issue with its output
is genuinely useful.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by Synology. "Synology" and "Hyper Backup" are trademarks
of Synology Inc., used here only to describe what this software reads.
