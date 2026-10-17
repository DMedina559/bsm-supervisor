# Supervisor releases

Development builds report `0.1.0.dev0` plus their recorded source commit. Release
packages derive their version from a normalized Python version tag, for example
`v0.1.0b1` or `v0.1.0`. There is no second package version to edit for a release.

The release workflow runs tests on Python 3.11 and 3.14, runs formatting/type
checks, builds a wheel and source distribution, and verifies the installed wheel
version and console assets. After those checks it creates a GitHub release with
generated release notes, packages, bootstrap scripts, `release.json`, and
`SHA256SUMS`. Prerelease tags create prereleases. Packages are published to
GitHub Releases.

Release bootstrap assets default to the release's exact commit. Repository
scripts select the latest stable release by default. Development source requires
`--ref dev`. For a reproducible existing install,
pass the release tag or exact commit to the updater:

```bash
bsm-supervisor supervisor-update --ref v0.1.0b1 \
  --maintenance-confirmed --wait
```

This command resolves the ref and pins the complete source commit before
installation. Release-tag updates preserve the tag version in package metadata while pinning
the source commit. Release bootstrap assets carry both the commit and version;
explicit bare commit/development installations report development metadata. `version` shows package and source information.
Private repositories require a GitHub token with repository contents read
access; credentials are not embedded in release assets.

Before creating a release tag, review the changes and validate a disposable
Proxmox LXC using [the live validation procedure](validation.md). After validation, push the version tag to trigger the release workflow.


Releases are experimental. Release notes identify this status, and the console
and README display it. Source installers, wheels and source distributions include
the license; installed console assets are checked during packaging. No tag or
public release is created by ordinary branch updates.


## Retry publication or attach assets to an existing release

In GitHub Actions, open **Release**, choose **Run workflow** on `main`, and enter
the existing tag, such as `v0.1.0b1`. This rebuilds and validates the tag's exact
commit using the current workflow. It leaves the tag unchanged.

If the release already exists, the workflow uploads the generated assets and
replaces same-named assets. Its title, description, draft status and prerelease
setting are preserved. Missing releases are created with generated notes and the
experimental notice. Publication stops if the tag moves during the build.

Rerunning an older failed workflow uses its original workflow definition. Use
**Run workflow** from the updated `main` branch when repairing a failure in the
publication workflow itself.
