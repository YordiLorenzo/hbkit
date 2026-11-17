# Notes for agents

## Upstream packaging PRs

The maintainer's agent owns the PRs that package hbkit in other projects, such as
https://github.com/NixOS/nixpkgs/pull/550225 (open on 2026-11-17). It watches them, answers reviewers, updates and
rebuilds the package, and asks again when a reviewer goes quiet. It does not hand them back to the maintainer. Only a
step that needs the maintainer's own account, such as a post on NixOS Discourse, goes to the maintainer.
