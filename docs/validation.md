# Validate a disposable Proxmox deployment

The host validation script exercises installation, guided setup, authenticated
readiness before first-user creation, setup reruns, checkpoint capture and
restoration, encrypted checkpoint export/import, console availability while BSM
is stopped, schedule installation/execution/removal, failed startup recovery,
supervisor updates, and service startup
after a full container reboot.

Run it from a supervisor checkout on a Proxmox VE 9.2+ host as root. Choose an
unused cluster guest ID and a full BSM commit that supports local health
authentication. The provisioner rejects existing VMs and containers. The test
creates and retains an unprivileged LXC; it never destroys a guest or reuses an
existing deployment.

Preview the lifecycle without making host or container changes:

```sh
CT_ID=900 # Example: choose an unused ID for the test container.
BSM_COMMIT=FULL_40_CHARACTER_BSM_COMMIT
bash scripts/validate-proxmox.sh --ctid "$CT_ID" --bsm-commit "$BSM_COMMIT" \
    --source-dir "$PWD" --dry-run
```

Replace the commit placeholder with an actual full SHA, then run:

```sh
bash scripts/validate-proxmox.sh --ctid "$CT_ID" --bsm-commit "$BSM_COMMIT" \
    --source-dir "$PWD" --disposable-confirmed
```

Use `--storage POOL` and `--template-storage POOL` when the defaults do not match
the host. Source files are copied from the local supervisor checkout; repository
credentials, Git metadata, and local virtual environments stay on the host.
BSM is built from its official repository at the supplied exact commit. The test
explicitly permits that source build.

The script deliberately breaks only the disposable installation's Python link
for its failed-startup check, restores it, and acknowledges that known failed
service operation after stopping the unit. Checkpoint restoration verifies a
controlled data marker and discards the test's retained pre-restore contents.
The supervisor update uses the copied checkout, keeps the BSM selection, and
verifies readiness again. Reboot testing enables the BSM unit in this test LXC.

Successful completion prints a validation result. On failure, the LXC and source
checkout remain available for inspection. Check the failing command, service
journal, `doctor`, and `history` before retrying individual steps. The creation
script will refuse to run again against that existing ID.

## Optional manual CI run

The **Proxmox LXC Validation** workflow runs only through manual dispatch. It
requires a dedicated self-hosted Linux runner on a Proxmox host, labelled
`proxmox-bsm-validation`, with root access, and uses the `proxmox-validation`
environment. Supply an unused container ID, a full BSM commit, and storage names.
The workflow retains its validation log as an artifact and leaves the container
for inspection. Normal push/PR CI does not access a Proxmox host.

Python unit tests and shell validation run in normal CI on Python 3.11 and 3.14.
Those checks simulate service/provisioning boundaries; a successful live host run
is separate evidence and must not be inferred from unit-test results.
