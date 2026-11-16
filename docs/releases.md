# Supervisor releases

Development builds continue from the latest reachable release tag and report
their recorded source commit separately. Release packages derive their version
from a normalized Python version tag, for example
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

## Package versions

The supervisor derives its package version from Git tags with `setuptools-scm`,
using the same scheme as BSM. An exact `v0.1.0` tag builds as `0.1.0`; one commit
later builds as `0.1.1.dev1`. After `v0.2.0b1`, the next commit builds as
`0.2.0b2.dev1`. Development versions omit the local Git suffix; the `version`
command reports the installed source revision separately.

Build from a Git checkout with full tag history, a GitHub source archive containing
expanded version metadata, or a published source distribution. A source copy
without this metadata cannot determine a reliable version. CI fetches full
history. GitHub archive metadata is expanded through `.gitattributes` so branch
installs in the LXC retain their development version.

Release builds and pinned release installers use the validated tag version.
The installer retains compatibility with release archives that predate SCM
versioning. No manual edit of the Python version constant is needed for a new
release. Installing or updating the package is required to update the installed
version shown by the CLI and web console.
