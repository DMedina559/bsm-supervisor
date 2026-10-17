# Off-host checkpoint copies

Checkpoint exports copy an existing verified checkpoint without stopping BSM.
Creating a new checkpoint still requires an offline capture. An export contains
the complete saved manifest and payload; configuration may include secrets.
It does not contain supervisor credentials, the supervisor database, or installed
BSM Python environments. Keep a separate copy of the supervisor configuration
and record the selected BSM version/source for host disaster recovery.

Export to an existing mounted backup directory using an absolute path:

```bash
bsm-supervisor checkpoint-export CHECKPOINT_ID /mnt/backups/saved.bsmcheckpoint
```

Use `--require-mount /mnt/backups` to refuse export when the expected mount is
absent or changes during the operation. The destination must be inside that mount.
Free-space checks reserve staging capacity before transfer.

The destination is never overwritten, and is published as a mode-0600 file only
after completion. Plain archives are ZIP files with a manifest and file payloads;
links and ownership metadata are represented in the manifest. Hash verification
detects corrupted payloads; an unencrypted archive is not proof of its sender.
Transfer completed archives to another machine with your existing backup tools.
The supervisor does not manage network-storage credentials or mount shares.

## Optional encryption

The LXC installer includes encryption support. Manual installations need the
`transfer` or `all` package extra. Create a private passphrase file without putting
the secret in shell history:

```bash
install -m 0600 /dev/null /root/checkpoint-passphrase
read -rsp 'Checkpoint passphrase: ' backup_passphrase
printf '%s\n' "$backup_passphrase" > /root/checkpoint-passphrase
unset backup_passphrase
bsm-supervisor checkpoint-export CHECKPOINT_ID \
  /mnt/backups/saved.bsmcheckpoint.enc \
  --passphrase-file /root/checkpoint-passphrase
```

Use a long, unique passphrase of at least 16 bytes. The file must be owned by the
operator, private, regular, and not a symlink. Keep a recoverable copy separately
from the LXC and backup archive. Losing it prevents recovery.

Encrypted format version 1 uses AES-256-GCM with a fresh 16-byte salt, 12-byte
nonce, 16-byte authentication tag, and scrypt (`N=32768`, `r=8`, `p=1`). The
`BSMENC01` header is authenticated along with the encrypted archive. Encryption
streams files rather than loading worlds into memory. Plaintext export staging
is confined to private supervisor storage; encrypted exports write only ciphertext
to the destination filesystem. Import decrypts into a
private temporary directory and verifies authentication before reading the ZIP.
Temporary plaintext needs local free space and is removed on normal failure or
completion; storage-level encryption is needed to protect local disk remnants.

## Import and recovery

```bash
bsm-supervisor checkpoint-import /mnt/backups/saved.bsmcheckpoint.enc \
  --passphrase-file /root/checkpoint-passphrase
bsm-supervisor checkpoints
bsm-supervisor restore-plan CHECKPOINT_ID
```

Import authenticates encrypted archives, validates configured root coverage,
checks declared sizes and every payload hash, and publishes only after complete
verification. It rejects traversal, unexpected or duplicate archive members,
children beneath symlinks, special payload files, and existing checkpoint IDs.
The default payload limit is 100 GiB; set `--max-bytes` explicitly if required.
Allow space for both decrypted archives and staged payloads during import.

Import does **not** restore data, install BSM, select a version, or rewrite saved
paths/ownership for another host. For a replacement LXC, configure the same
coverage paths, prepare compatible service-user numeric IDs, and install the
matching BSM version before reviewing a restore. Do not restore an untrusted
archive. Continue with [checkpoint recovery](checkpoints.md) only after reviewing
the plan and stopping BSM.

Encrypted and plain copies follow the same checkpoint format and local recovery
rules. External databases remain outside coordinated checkpoint support.
