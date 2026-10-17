# Supervisor releases

Development builds report `0.1.0.dev0` plus their recorded source commit. Release
packages derive their version from a normalized Python version tag, for example
`v0.1.0b1` or `v0.1.0`. There is no second package version to edit for a release.
The license remains `AGPL-3.0-only`.

The release workflow runs tests on Python 3.11 and 3.14, runs formatting/type
checks, builds a wheel and source distribution, and verifies the installed wheel
version and console assets. After those checks it creates a GitHub release with
generated release notes, packages, bootstrap scripts, `release.json`, and
`SHA256SUMS`. Prerelease tags create prereleases. No release has been published
just by adding this workflow, and it does not publish to PyPI.

Release bootstrap assets default to the release's exact commit. Repository
scripts retain the development default. For a reproducible existing install,
pass the release tag or exact commit to the updater:

```bash
bsm-supervisor supervisor-update --ref v0.1.0b1 \
  --maintenance-confirmed --wait
```

This command resolves the ref and pins the complete source commit before
installation. The source installer builds from that commit, so development
metadata may still appear alongside the recorded commit; packaged GitHub release
wheels report the tag version. `version` shows package and source information.
Private repositories still require a GitHub token with repository contents read
access; credentials are not embedded in release assets.

Before creating a release tag, review the changes and validate a disposable
Proxmox LXC using [the live validation procedure](validation.md). The normal CI
suite does not replace a real systemd/LXC lifecycle check. Tags and publishing
remain a maintainer action; the workflow cannot establish live-host support on
its own.
