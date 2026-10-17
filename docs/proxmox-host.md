# Proxmox host setup

Launch the host script from the **Proxmox host shell**, rather than inside an
existing LXC. It provides a community-script style Default/Advanced wizard and
storage selection menus. It uses `whiptail` when available and ordinary terminal
prompts otherwise; it does not install UI packages or source remote helper scripts
on the host.

## Launch the wizard

Because this repository is private, download it with a read-only GitHub token:

```bash
read -r -s -p 'GitHub token: ' GH_TOKEN
printf '\n'
export GH_TOKEN
installer=$(mktemp /tmp/bsm-supervisor-proxmox-XXXXXXXX.sh)
if [[ $GH_TOKEN =~ ^[A-Za-z0-9_]+$ ]] && \
    printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$GH_TOKEN" | \
    curl --config - --fail --silent --show-error --proto '=https' \
    'https://api.github.com/repos/DMedina559/bsm-supervisor/contents/scripts/provision-proxmox.sh?ref=dev' \
    -o "$installer"; then
    bash "$installer"
fi
rm -f "$installer"
unset GH_TOKEN
```

With a local checkout, run `bash scripts/provision-proxmox.sh` directly. No
arguments and a terminal launch the wizard automatically. `--interactive` also
opens the wizard when combining options. Pin both download URLs/references to a
full commit SHA instead of `dev` if you want an immutable source revision.

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
supervisor source revision. Storage menus appear in both modes.

Without an explicit template, the script refreshes the appliance catalog and
selects the newest Debian 13 standard amd64 image on Proxmox VE 9+, falling
back to Debian 12. Proxmox VE 8 selects Debian 12 for host compatibility.
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
    --ip 192.168.10.20/24 --gateway 192.168.10.1 \
    --nameserver 192.168.10.2 --vlan 10
```

Use your actual network/storage values. Preview any command with `--dry-run`.
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
setup, and readiness-token configuration in [LXC installation](install-lxc.md).
There is no supervisor web dashboard yet.

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

The command plans, validation, wizard, and archive checks are tested with a fake
Proxmox command interface. A real Proxmox host/LXC run remains necessary for field
validation. The implementation follows the official
[`pct` documentation](https://pve.proxmox.com/pve-docs/pct.1.html).

## Root DNS works but APT cannot resolve

Older provisioner revisions applied the host's private `umask 077` to `pct`.
Proxmox template extraction can inherit that mask and leave `/etc` inaccessible
to the `_apt` download account. Current revisions run every `pct` command with
`umask 022` inside a subshell while retaining private host credential permissions.
The LXC installer also checks resolver-file access as `_apt`, stops on any APT
index-update error, and skips unnecessary recommended distribution packages.

For an affected fresh container, inspect permissions and test the download user
from the host (replace 101 with the actual ID):

```sh
pct exec 101 -- stat -c '%a %U:%G %n' / /etc /etc/resolv.conf /etc/nsswitch.conf /etc/hosts
pct exec 101 -- runuser -u _apt -- getent ahostsv4 deb.debian.org
```

If `/etc` or those public resolver files have restrictive permissions, restore
normal access to those specific paths, without recursive permission changes:

```sh
pct exec 101 -- chmod 755 /etc
pct exec 101 -- chmod 644 /etc/resolv.conf /etc/nsswitch.conf /etc/hosts
pct exec 101 -- runuser -u _apt -- getent ahostsv4 deb.debian.org
```

Once that succeeds, retry the retained bootstrap as described above. For a fresh
container whose BSM unit has not been created yet, include `--install-bsm-unit`.
A root-only successful lookup does not establish that APT can resolve names.
