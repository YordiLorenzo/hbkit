# hbkit product marketing context

Read this before writing any hbkit copy (site, README, release notes, posts). Last updated 2026-11-11.

## What it is

hbkit is an open reader for your own Synology Hyper Backup (`.hbk`) archives. It restores files on macOS (Apple silicon and Intel) and Linux with no NAS and no Synology software. Free, MIT licensed.

Second sentence, everywhere: it checks every chunk against the archive's own MD5 and CRC32 and never writes to the archive.

## Framing (decided by the maintainer, 2026-11-11)

- Lead with "an open reader for your own backups". Never lead with "reverse engineered".
- FORMAT.md is "a public description of the .hbk format", written so archives stay readable whatever happens to the NAS or the app.
- The Mac point is insurance, not an attack on Synology: Explorer's Mac and Linux builds are x86 only. One sentence, word for word: "Apple says Rosetta, which runs Intel apps on Apple silicon, stays available for all apps only through macOS 27."

## Two jobs, one per channel

- **Recovery** (search, damaged-backup threads): get your files out of a Hyper Backup without the NAS.
- **Drill** (r/synology, forums, World Backup Day on March 31): check on a second computer that your backup restores, before you need it.

## Who it is for

Synology owners with a Hyper Backup on a USB disk, another NAS, or an S3, B2 or R2 bucket. Mostly home, homelab and small-office IT. Linux is about 73% of PyPI downloads with a known OS (pypistats, 2026-11-10), macOS about 19%.

## Rules every page keeps

- Copy rule, word for word: "hbkit never writes to the archive. If it is your only copy, copy the whole .hbk folder to another disk first and run hbkit on the copy."
- Failing disk: "If the disk is slow, clicks or shows SMART errors, stop. Image it or send it to a recovery lab, then run hbkit on the copy."
- Synology C2: "not tested yet". Explorer stays the right tool for C2, key-file unlock and older RSA archives.
- Windows: CI restores test archives on Windows; no backup from a real NAS has been restored on Windows yet.
- The big reference archive is "3 TB".
- No em or en dashes, no marketing words (seamless, powerful, effortless), no "guaranteed". Plain short sentences.
- Text in the maintainer's own voice (Reddit, forums, emails): lowercase, short, a few typos, and it says he is the author.

## Proof (quote only with consent on record)

- A 3 TB DSM 7 / Hyper Backup 4.1.2 archive restored byte-exact (README).
- R2, 3 TB: `hbk mount` cut opening the archive from 9 min 21 s to 1.8 s (README).
- B2: a 74 MB video rebuilt exactly from a mounted bucket, 9,072 chunks verified (PR #3).
- Issue #6, wingsoon2, consent to be named given 2026-11-11: 465,000 files (754 GB) restored from a backup on a NAS hit by C3RB3R ransomware.
- r/synology thread 1wkt1p5, u/user_immortal: about 90% of their data back after Synology support said it was lost. **No consent yet**: asked 2026-11-11.

## Competition

- **Hyper Backup Explorer** (Synology): free, GUI, no command line, last release 3.0.1 on 2023-04-13. Also reads any backup version and works on a mounted bucket. Never claim those as hbkit-only.
- **Callido Restore**: macOS/Windows GUI, MIT, started 2026-10-30. Since 1.3.1 it reads archives that ransomware partly encrypted. We have not reviewed its code.

## Where the copy lives

- Site: `site/` (homepage plus guides at /restore-without-nas/, /damaged-backup/, /mac/).
- Go-to-market plan and copy drafts from 2026-11-10: kept outside the repo; facts that matter are in this file.
