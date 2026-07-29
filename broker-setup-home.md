# Host broker — home-directory setup (scratch note, safe to delete)

Everything lives under `~/.cld/broker/`. No `/etc`, no `/opt`. Run all commands
from the repo root. Replace the two paths in step 3 with your real repo + `.env`.

Prereq: `runtests:latest` is built (`runtests/build.sh`).

```bash
BROKER="$HOME/.cld/broker"
```

## 1. Directory + keys

```bash
mkdir -p "$BROKER" && chmod 700 "$BROKER"
host-broker/keygen.sh "$BROKER"
```

Writes into `$BROKER/`: `broker_key(.pub)`, `broker_authorized_keys`,
`broker_ssh_host_ed25519_key(.pub)`.

## 2. known_hosts (for the container side later)

```bash
printf '[host.docker.internal]:2222 %s\n' \
  "$(awk '{print $1, $2}' "$BROKER/broker_ssh_host_ed25519_key.pub")" \
  > "$BROKER/known_hosts"
```

## 3. Broker script + config + control script

```bash
cp host-broker/host-broker.sh "$BROKER/host-broker.sh" && chmod +x "$BROKER/host-broker.sh"
cp host-broker/host-broker.conf.sample "$BROKER/host-broker.conf"
${EDITOR:-nano} "$BROKER/host-broker.conf"

# Put brokerctl on PATH so operation is just `brokerctl start|restart|shutdown`.
mkdir -p ~/.local/bin
ln -sf "$PWD/host-broker/brokerctl.sh" ~/.local/bin/brokerctl
```

Set in `host-broker.conf` (broker-wide only — the repo and its `.env` are
resolved per request from the calling master, so nothing per-repo goes here):

```sh
RUNTESTS_IMAGE=runtests:latest
# The forced command runs with a minimal PATH; make jj + docker reachable:
PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
```

## 4. sshd config (home paths)

```bash
GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')  # usually 172.17.0.1
cat > "$BROKER/sshd_cld_broker.conf" <<EOF
ListenAddress $GW
Port 2222
HostKey $BROKER/broker_ssh_host_ed25519_key
AllowUsers $USER
AuthorizedKeysFile $BROKER/broker_authorized_keys
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ForceCommand $BROKER/host-broker.sh
SetEnv CLD_BROKER_CONF=$BROKER/host-broker.conf
PermitTTY no
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
UsePAM no
PidFile $BROKER/sshd.pid
EOF
```

`SetEnv CLD_BROKER_CONF=…` is what lets the broker find its config in home
(its built-in default is `/etc/cld/host-broker.conf`).

## 5. Operate it with `brokerctl`

Setup is done. From here operation is seamless — `brokerctl` launches sshd
detached (no `-D`, so closing the shell won't kill it), tracks it by PID file,
and logs to `$BROKER/sshd.log`:

```bash
brokerctl start      # start (idempotent; no-op if already running)
brokerctl status     # running/stopped + listen address
brokerctl restart    # after editing config, or to pick up a new runtests image
brokerctl shutdown   # stop
brokerctl logs 50    # tail the sshd log
```

Port 2222 (>1024) needs no root. If `start` reports an error mentioning
`/run/sshd`, create that dir once (`sudo mkdir -p /run/sshd`) and retry; the
broker still runs as `$USER` via `AllowUsers`, so it owns your repos + docker.
`brokerctl` reads `$CLD_BROKER_DIR` (default `~/.cld/broker`).

## 6. Smoke test (host → broker, no real master needed)

The broker resolves the repo from a running master's `org.cld.repo-root` label,
so stand up a throwaway container carrying that label + a matching bookmark:

```bash
REPO=/abs/path/to/lide-api
docker run -d --name cld_master_smoke --label org.cld.repo-root="$REPO" alpine sleep 300
jj -R "$REPO" bookmark create cld_master_smoke -r @

payload=$(printf '%s\0' -q -x | base64 -w0)                # pytest args: -q -x
GW=$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}')
ssh -i "$BROKER/broker_key" -o StrictHostKeyChecking=no \
    -p 2222 "$USER@$GW" -- "run-tests cld_master_smoke $payload"

# should stream: jj workspace -> poetry install -> pytest. Denials to confirm the boundary:
ssh -i "$BROKER/broker_key" -o StrictHostKeyChecking=no -p 2222 "$USER@$GW" -- "deploy x $payload"                # denied (bad action)
ssh -i "$BROKER/broker_key" -o StrictHostKeyChecking=no -p 2222 "$USER@$GW" -- "run-tests ../etc/passwd $payload"  # denied (bad session)
ssh -i "$BROKER/broker_key" -o StrictHostKeyChecking=no -p 2222 "$USER@$GW" -- "run-tests cld_master_none $payload"  # denied (no such master)

docker rm -f cld_master_smoke; jj -R "$REPO" bookmark delete cld_master_smoke   # cleanup
```

`StrictHostKeyChecking=no` is only for this test; the container uses the pinned
`$BROKER/known_hosts` from step 2.

## 7. Wire the container (master)

In your project `.cld/config.toml` (or `~/.config/cld/config.toml`):

```toml
host_broker_key = "~/.cld/broker/broker_key"
host_broker_endpoint = "host.docker.internal:2222"
host_broker_known_hosts = "~/.cld/broker/known_hosts"
```

The `host-run` wrapper lives in `container-init.sh` (baked into the base image),
so rebuild it once — `cld build` — for `host-run` to appear in master. Then from
inside master: `host-run -k login -x tests/` (or `host-run --action <name> …`).
