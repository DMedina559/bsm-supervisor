# Install from an LXC console

Run inside a Debian 12+ or Ubuntu 24.04+ systemd container as root. Python 3.11+
is required. The installer installs distribution packages, creates a permanent
supervisor virtual environment, and provides launchers in `/usr/local/bin` and
`/usr/bin`. The latter supports `pct enter` shells whose PATH omits local binaries.
The installer verifies the command with a minimal `/usr/bin:/bin` PATH and refuses
to replace unrelated commands at either launcher path.
To create the container from the Proxmox host instead, use the
[guided host provisioner](proxmox-host.md).

## Private repository download

The repository is private, so a plain unauthenticated raw URL will return 404.
Use a GitHub token with read access to this repository. Paste the following into
the LXC console. The token is read without echo and is not placed in command
arguments or saved in shell history.

```bash
apt-get update
apt-get install -y curl ca-certificates
read -r -s -p 'GitHub token: ' GH_TOKEN
printf '\n'
export GH_TOKEN
installer=$(mktemp /tmp/bsm-supervisor-install-XXXXXXXX.sh)
if [[ $GH_TOKEN =~ ^[A-Za-z0-9_]+$ ]] && \
    printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$GH_TOKEN" | \
    curl --config - --fail --silent --show-error --proto '=https' \
    'https://api.github.com/repos/DMedina559/bsm-supervisor/contents/scripts/install-lxc.sh?ref=dev' \
    -o "$installer"; then
    bash "$installer" --ref dev --install-bsm-unit
fi
rm -f "$installer"
unset GH_TOKEN
```

Use a full commit SHA instead of `dev` in both places to install a fixed revision.
`dev` is the current development branch. Downloading the script first also lets
you inspect it before running it.

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

Match the generated ExecStart, conditions, and environment settings when adopting
a unit. Merely keeping an old BSM service command will fail the offline checks.
The installer neither enables nor starts BSM. There is no always-running
supervisor daemon in this milestone: the supervisor is the operator CLI, journal,
checkpoint manager, and guarded systemd integration.

## First BSM startup

For a fresh container, prepare a prerelease and inspect its installation ID:

```sh
bsm-supervisor install prerelease
bsm-supervisor installations
bsm-supervisor activate INSTALLATION_ID --offline-confirmed
bsm-supervisor start
```

Replace `INSTALLATION_ID` with the ID printed by `installations`. Finish BSM's own
initial account/setup flow, then create a BSM bearer token for authenticated
readiness. Save it as a root-owned regular file with mode 0600, and set
`api_token_file` in supervisor configuration. The installer cannot create an
application token before BSM has been initialized. BSM 4.0 is required for the
audited coordinated upgrade/runtime shutdown flow.

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

See [updating the supervisor](update-supervisor.md) for a complete private-repository
download and the managed environment/unit migration.

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

Shell validation and simulated upgrade recovery tests run in CI. A complete root
bootstrap, application setup, and upgrade on a real Proxmox LXC still need field
validation.

See [BSM listener and Proxmox environment](listener-environment.md) for LAN binding,
native environment settings, and updating an existing managed unit.
