#!/usr/bin/env bash
# Generate the dedicated broker client keypair + the sshd instance host key, and
# assemble the authorized_keys file. Needs no root; writes everything under a dir
# you control (default: ./broker-keys).
set -euo pipefail
OUT="${1:-./broker-keys}"
mkdir -p "$OUT"

[ -f "$OUT/broker_key" ] || \
    ssh-keygen -t ed25519 -N '' -C 'cld-broker' -f "$OUT/broker_key"
[ -f "$OUT/broker_ssh_host_ed25519_key" ] || \
    ssh-keygen -t ed25519 -N '' -C 'cld-broker-host' -f "$OUT/broker_ssh_host_ed25519_key"

# `restrict` is belt-and-suspenders next to sshd's ForceCommand.
printf 'restrict %s\n' "$(cat "$OUT/broker_key.pub")" > "$OUT/broker_authorized_keys"

hostpub=$(awk '{print $1, $2}' "$OUT/broker_ssh_host_ed25519_key.pub")
cat <<EOF

Keys written to $OUT/
  broker_key                    -> mount into master (cld broker_key)
  broker_key.pub                -> public half
  broker_authorized_keys        -> install as AuthorizedKeysFile (see sshd_cld_broker.conf)
  broker_ssh_host_ed25519_key   -> HostKey for the dedicated sshd instance

known_hosts line for the container (cld broker_known_hosts); fix host:port
if you changed them:

  [host.docker.internal]:2222 $hostpub
EOF
