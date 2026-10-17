# Proxmox host setup

Requires Proxmox VE **9.2+** with native container environment support.

Launch the host script from the **Proxmox host shell**, rather than inside an
existing LXC. It provides a community-script style Default/Advanced wizard and
storage selection menus. It uses `whiptail` when available and ordinary terminal
prompts otherwise; it does not install UI packages or source remote helper scripts
on the host.

## Launch the wizard

Download the latest stable release launcher. Public downloads require no GitHub token:

```bash
installer=$(mktemp /tmp/bsm-supervisor-proxmox-XXXXXXXX.sh)
if curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 20 --max-time 300 \
    "https://github.com/DMedina559/bsm-supervisor/releases/latest/download/provision-proxmox.sh" \
    -o "$installer"; then
    bash "$installer" --interactive
fi
rm -f "$installer"
```

With a local checkout, run `bash scripts/provision-proxmox.sh` directly. No
arguments and a terminal launch the wizard automatically. `--interactive` also
opens the wizard when combining options. Release launcher assets pin their source commit. To select a prerelease, use
`releases/download/TAG` in the URL. The latest-stable URL requires a published
stable release. Development source is an explicit `--ref dev` choice. Private
source downloads accept `GH_TOKEN` with Contents: read-only access.

1. Choose **Default** or **Advanced** settings.
2. Select container disk storage and template storage from this node's active
   storage pools.
3. Review the container summary and confirm creation.
4. The script prepares the template, creates and starts the LXC, waits for DNS,
   and runs the supervisor installer inside it.

Default resources are 2 CPU cores, 2048 MB RAM, 512 MB swap, and a 16 GB root disk.
The hostname is `bsm-supervisor`; networking uses DHCP on `vmbr0`, with IPv6 left
unconfigured and the interface firewall flag enabled. Container autostart is off.
The next available cluster guest ID is suggested. Advanced mode lets you change
ID, hostname, resources, bridge, static IPv4/gateway, DNS, VLAN, autostart, and the
supervisor source revision and BSM bind address. Storage menus appear in both modes.
The default BSM bind is `0.0.0.0`, supplied through the native container environment
as `HOST`, alongside `PORT` and all four public BSM bootstrap variables. See [listener configuration](listener-environment.md)
for the complete variable table and existing-container migration.
Pass `--bsm-env-file /root/bsm-env.json` to customize all public variables; use
[the example JSON](../config/proxmox-env.example.json) as a starting point.

Without an explicit template, the script refreshes the appliance catalog and
selects the newest Debian 13 standard amd64 image, falling back to Debian 12.
An x86_64 host is required for automatic template selection. The template cache
may be updated before the creation confirmation; a cancelled wizard never creates
a container. Existing templates are reused by the Proxmox appliance manager.

## Command-line mode

Arguments run noninteractively unless `--interactive` is present. Private source
downloads use `GH_TOKEN`, or a hidden prompt when attached to a terminal.

```sh
bash scripts/provision-proxmox.sh --dry-run
bash scripts/provision-proxmox.sh --ctid 120 --storage local-lvm \
    --bridge vmbr0 --cores 4 --memory 4096 --disk 64 --onboot
bash scripts/provision-proxmox.sh --ctid 120 --storage local-zfs \
    --ip 192.0.2.20/24 --gateway 192.0.2.1 \
    --nameserver 192.0.2.2 --vlan 10
```

The static-address example uses documentation-only addresses. Substitute your actual network/storage values and an unused guest ID. Preview any command with `--dry-run`.
Dry runs only inspect the host and print a quoted `pct create` command: they do
not refresh/download templates, request credentials, create guests, or install
anything. Automatic template previews use the cached `pveam available` catalog;
if it is empty, refresh it yourself or pass an existing template volume.

Use an already downloaded supported Debian/Ubuntu template with:

```sh
bash scripts/provision-proxmox.sh \
    --template local:vztmpl/YOUR_TEMPLATE.tar.zst --dry-run
```

Use `--source-dir /absolute/path/to/bsm-supervisor` to avoid GitHub downloads.
Only package installation inputs are copied into the LXC. The GitHub token stays
on the host and is not passed in `pct` arguments or copied into the container.
The host needs its standard Proxmox CLI tools, Python 3, tar, and curl for remote
source downloads. No host APT sources, firewall rules, bridges, or existing guests
are modified. The new interface's firewall flag does not open any firewall ports;
allow the BSM web and Bedrock ports appropriate for your configuration yourself.

## Result and recovery

The container is always **unprivileged**, without nesting or host bind mounts.
The cluster-wide ID check includes both VMs and containers; `pct create` also
rejects a competing allocation. An existing guest is never adopted or overwritten.
A host lock prevents simultaneous runs of this provisioner from creating guests.

After completion, enter the new container with `pct enter ID`. The supervisor,
its configuration/journal, and the managed BSM unit are installed. BSM itself is
not installed, enabled, or started: finish the version selection, BSM account
setup in [LXC installation](install-lxc.md). Run `bsm-supervisor setup --offline-confirmed` inside the new container.

If creation/start/bootstrap fails, the script reports the ID and retains the
container for inspection. It never automatically destroys a container or disk.
If bootstrap files were copied, they remain at `/root/bsm-supervisor-bootstrap`.
From `pct enter ID`, retry the installer using that checkout. If the BSM unit was
already written, omit `--install-bsm-unit` on retry:

```sh
bash /root/bsm-supervisor-bootstrap/scripts/install-lxc.sh \
    --source-dir /root/bsm-supervisor-bootstrap
```

After a successful retry, remove the bootstrap directory yourself. Do not run the
host creation script again with an existing ID; it intentionally refuses reuse.

## Troubleshooting

For provisioning options, use `bash scripts/provision-proxmox.sh --help` and the
[Proxmox pct reference](https://pve.proxmox.com/pve-docs/pct.1.html).

If package installation reports DNS failures, test inside the container as both
root and APT's download account:

```sh
getent ahostsv4 deb.debian.org
runuser -u _apt -- getent ahostsv4 deb.debian.org
stat -c '%a %U:%G %n' / /etc /etc/resolv.conf /etc/nsswitch.conf /etc/hosts
```

If only the root lookup succeeds, check directory traversal and resolver-file
read permissions for `_apt`. Correct the specific inaccessible paths according
to the distribution defaults; avoid recursive permission changes. If both lookups
fail, inspect the container's DNS, route, bridge, and firewall configuration.
Retry the retained bootstrap after resolving the failure.
