# Git sources and installation cleanup

Git installs build the official `DMedina559/bedrock-server-manager` repository.
Install Git in the LXC first. Source builds execute upstream build code, so they
require an explicit opt-in:

```sh
bsm-supervisor install-git --branch dev --trust-source-build
bsm-supervisor install-git --tag v4.0.0 --trust-source-build
bsm-supervisor install-git --commit FULL_40_CHARACTER_SHA --trust-source-build
```

Use a tag that exists upstream; examples do not imply a release is published.
The supervisor resolves the reference, checks out its exact commit, builds in a
separate permanent virtual environment, checks dependencies, and records the
reference, complete commit, and computed package version. An existing commit
installation is never overwritten. Branch updates produce separate installations.
Build logs remain in the supervisor state directory. Failed unregistered build
environments are retained for inspection; this command does not delete them.

Git installation does not stop BSM, select the environment, or run migrations.
Use `activate ID --offline-confirmed` only during an offline maintenance window.
The coordinated `upgrade` command currently selects PyPI releases; Git builds
are not yet part of that automatic checkpoint/start/readiness workflow.

Remove an unused supervisor-created installation explicitly:

```sh
bsm-supervisor installation-remove ID --offline-confirmed --delete-confirmed
```

Removal requires the configured managed service to be stopped. It refuses the
selected environment, every environment referenced by a retained checkpoint,
unowned/manual environments, symlink roots, nested mounts, and special files.
Invalid checkpoint metadata blocks removal until investigated. Pending operations
and upgrade workflows also block cleanup through the common journal lock.

The environment is renamed into a private cleanup location under installations
before deletion, with a durable record in supervisor state. On ordinary deletion
failure, run the same command again. After controller termination, inspect history
and acknowledge the interrupted cleanup with `--service-checked`, then retry the
same ID. Never delete cleanup records manually while deletion is incomplete.
World data, configuration, and checkpoint payloads are outside the deletion scope.
Actual Proxmox LXC/systemd validation remains required before production use.
