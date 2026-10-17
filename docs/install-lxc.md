# Install from an LXC console

Run inside a Debian 12+ or Ubuntu 24.04+ systemd container as root. Python 3.11+
is required. The installer installs distribution packages, creates a permanent
supervisor virtual environment, and provides launchers in `/usr/local/bin` and
`/usr/bin`. The latter supports `pct enter` shells whose PATH omits local binaries.
The installer verifies the command with a minimal `/usr/bin:/bin` PATH and refuses
to replace unrelated commands at either launcher path.
To create the container from the Proxmox host instead, use the
[guided host provisioner](proxmox-host.md).

## Download and install

Download the latest stable release installer inside the LXC. Public downloads
require no GitHub token. Inspect the downloaded script before executing it when
reviewing a deployment.

```bash
apt-get update
apt-get install -y curl ca-certificates
installer=$(mktemp /tmp/bsm-supervisor-install-XXXXXXXX.sh)
if curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 20 --max-time 300 \
    "https://github.com/DMedina559/bsm-supervisor/releases/latest/download/install-lxc.sh" \
    -o "$installer"; then
    bash "$installer" --install-bsm-unit
fi
rm -f "$installer"
```

For an explicit release, including a prerelease, replace `releases/latest/download`
with `releases/download/TAG`. The latest-stable URL requires a published stable
release. To use a source checkout, run `bash scripts/install-lxc.sh --source-dir
/absolute/path/to/checkout`; `--ref dev` explicitly selects development source.
For private source downloads, supply `GH_TOKEN` with Contents: read-only access
or use the hidden prompt after an authentication failure.

The installer fetches the source archive over HTTPS, rejects archive links and
unsafe paths, installs the supervisor, checks dependencies, validates configuration,
and initializes the supervisor journal. It creates:

| Location | Purpose |
| --- | --- |
| `/opt/bsm-supervisor/versions/` | Permanent supervisor environments |
| `/opt/bsm-supervisor/current` | Current supervisor CLI environment |
| `/etc/bsm-supervisor/config.toml` | Root-only supervisor configuration |
| `/var/lib/bsm-supervisor/` | Private journal, plans, logs and checkpoints |
| `/opt/bsm/installations/` | BSM environments and selection |
| `/srv/bsm` | Default BSM data directory, initially owned by `bsm` |

Existing configuration and BSM data ownership are preserved. Read and edit the
configuration when adopting an existing installation: the default `/srv/bsm`
may differ from your current paths. Declare external server/content directories
in `checkpoint_paths` before attempting coordinated upgrades.

`--install-bsm-unit` creates the managed BSM systemd unit only when no existing
unit is found; it refuses replacement before installing anything. Omit that flag
when adopting an existing unit. Generate and review a candidate with:

```sh
bsm-supervisor unit --user bsm > /tmp/bedrock-server-manager-webui.service
```

Before installing a replacement unit, stop BSM and generate its private environment
file. Follow the complete [managed unit procedure](update-supervisor.md#refresh-the-managed-unit);
rendering the unit alone does not create that file.
The installer neither enables nor starts BSM. The supervisor provides a local CLI, journal, checkpoint manager, and guarded systemd integration.

## First BSM startup

For a fresh container, run guided setup:

```sh
bsm-supervisor setup --offline-confirmed
```

Interactive setup offers registered installations and release channels. Without
a terminal or an existing installation, it prepares the prerelease channel.
Specify a compatible exact version/channel or `--installation INSTALLATION_ID`
to choose explicitly. The command installs a missing managed unit, selects the
environment, starts BSM, and prints listener URLs. Reruns preserve an existing
selection. See [administration](administration.md) for the full behavior.

Complete BSM's first-user setup in the browser. No user token is needed for
initial startup. BSM versions with local health authentication require no extra
credential setup; older versions need a user token for health checks and
coordinated upgrades. See [readiness authentication](upgrades.md#configure-readiness-and-startup-control).

Low-level commands remain available when adopting an existing installation:
`install`, `installations`, `activate ID --offline-confirmed`, and `start`.
Repeating `install` reuses a verified registered release without changing the
selection. Setup refuses to guess a version for existing unselected BSM data.

## Git upgrades

Build the selected branch/tag/commit first; this leaves the running BSM untouched:

```sh
bsm-supervisor install-git --branch dev --trust-source-build
bsm-supervisor installations
bsm-supervisor upgrade --installation git-FULL_COMMIT_SHA \
    --maintenance-confirmed --accept-migrations
```

Use the actual registered Git ID. A Git build with the same computed package
version requires `--allow-same-version`; older package versions are refused.
The workflow checks the installed metadata and Git provenance, gracefully stops
BSM, creates and pins a checkpoint, selects the Git environment, then requires
authenticated readiness. The durable recovery plan records the exact commit.
Resume never resolves a moving branch or silently downloads a replacement.
If the target is missing or its provenance changes, recovery stops for inspection.
Use `upgrade-recover` as described in [upgrades](upgrades.md), including explicit
checkpoint restoration when returning to the original version and data.

## Reinstalling the supervisor

See [updating the supervisor](update-supervisor.md) for the complete supervisor
download and managed unit refresh.

Run the same installer without `--install-bsm-unit` to preserve an existing unit.
Each run creates a fresh permanent supervisor environment and publishes the CLI
link only after installation/configuration checks pass. Earlier environments are
retained because generated BSM units can reference their guard executable.
Do not remove them without updating and verifying the unit first. Failed runs may
leave an unselected environment for inspection. An installer lock prevents two
bootstrap runs from modifying the setup concurrently.

For an existing local checkout, skip the repository download:

```sh
bash scripts/install-lxc.sh --source-dir "$PWD" --install-bsm-unit
```

See [BSM listener and Proxmox environment](listener-environment.md) for LAN binding,
native environment settings, and updating an existing managed unit.

## BSM installation access

BSM runs as its service account, so installed Python packages need read and directory
traversal access. The supervisor keeps package environments root-owned and does not
grant the service account write access. Installation child processes use a code
umask of `022`, independently of the console's private `0077` umask. Credentials,
operation logs and BSM data retain their private permissions.

For an environment installed by an older supervisor, inspect the selected Python
path and test access as the service account:

```sh
namei -l /opt/bsm/installations/current/bin/python
runuser -u bsm -- /opt/bsm/installations/current/bin/python -I \
  -c 'from importlib.metadata import version; print(version("bedrock-server-manager"))'
```

If package directories or files are inaccessible, repair permissions only within
the verified installation directory. Do not apply recursive permission changes to
BSM data, configuration, supervisor state, or an external interpreter target. A
`noexec` mount or a private ancestor directory requires separate correction. Updated
supervisors also normalize verified PyPI environments when reusing an installation.
