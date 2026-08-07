# Packaging

Recipes for package repositories that keep their definitions outside this repo. Each one
pins a released sdist from PyPI, so bumping a version means changing the version string and
the hash, nothing else.

| Target | Files here | Lives at |
| ------ | ---------- | -------- |
| Homebrew | — | [YordiLorenzo/homebrew-tap](https://github.com/YordiLorenzo/homebrew-tap) |
| AUR | `aur/PKGBUILD`, `aur/.SRCINFO` | `ssh://aur@aur.archlinux.org/hbkit.git` |
| nixpkgs | `nix/package.nix` | `pkgs/by-name/hb/hbkit/package.nix` |

## A note on liblz4

hbkit `dlopen()`s liblz4 rather than linking it, so a package that forgets the dependency
still builds and installs cleanly, then fails the first time someone extracts a file. Every
recipe here must declare it, and should ideally set `HBK_LZ4` to an absolute path so the
library resolves regardless of the loader search path.

The Homebrew formula wraps both entry points with `HBK_LZ4` for exactly this reason. Arch
and Nix put their libraries on the default search path, so a plain dependency is enough.

## AUR

```sh
git clone ssh://aur@aur.archlinux.org/hbkit.git
cp packaging/aur/PKGBUILD packaging/aur/.SRCINFO hbkit/
cd hbkit && git add PKGBUILD .SRCINFO && git commit -m "hbkit 0.4.2" && git push
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
