# Packaging

Recipes for package repositories that keep their definitions outside this repo. Each one
pins a released sdist from PyPI, so bumping a version means changing the version string and
the hash, nothing else.

| Target | Files here | Lives at |
| ------ | ---------- | -------- |
| Homebrew | — | [YordiLorenzo/homebrew-tap](https://github.com/YordiLorenzo/homebrew-tap) |
| AUR | `aur/PKGBUILD`, `aur/.SRCINFO` | `ssh://aur@aur.archlinux.org/hbkit.git` |
| nixpkgs | `nix/package.nix` | `pkgs/by-name/hb/hbkit/package.nix` ([PR #550225](https://github.com/NixOS/nixpkgs/pull/550225)) |

## A note on liblz4

hbkit `dlopen()`s liblz4 rather than linking it, so a package that forgets the dependency
still builds and installs cleanly, then fails the first time someone extracts a file. Every
recipe here must declare it, and should ideally set `HBK_LZ4` to an absolute path so the
library resolves regardless of the loader search path.

Homebrew and Nix both wrap the two entry points with `HBK_LZ4` for exactly this reason —
Nix has no global library directory at all, so the name alone resolves to nothing. Arch
puts `liblz4.so.1` on the default loader path, so a plain dependency is enough there.

The same shape caused a worse bug: workers are spawned as `sys.executable -m hbkit.runner`,
and a wrapper script leaves `sys.executable` pointing at an interpreter that cannot import
hbkit. Fixed in 0.4.3 by passing the parent's `sys.path` down. Any recipe that wraps the
entry points needs at least 0.4.3.

## AUR

```sh
git clone ssh://aur@aur.archlinux.org/hbkit.git
cp packaging/aur/PKGBUILD packaging/aur/.SRCINFO hbkit/
cd hbkit && git add PKGBUILD .SRCINFO && git commit -m "hbkit 0.4.3" && git push
```

`.SRCINFO` must match `PKGBUILD`; regenerate it on Arch with `makepkg --printsrcinfo >
.SRCINFO`. The AUR rejects a push where the two disagree.

## nixpkgs

`nix/package.nix` goes to `pkgs/by-name/hb/hbkit/package.nix` in a nixpkgs fork. It
references a maintainer that must exist first, so `maintainers/maintainer-list.nix` needs:

```nix
  yordilorenzo = {
    email = "yordilorenzo@gmail.com";
    github = "YordiLorenzo";
    githubId = 7012112;
    name = "Yordi de Kleijn";
  };
```

Build it before opening the PR:

```sh
nix-build -A hbkit
nix run -f . hbkit -- --version
```
