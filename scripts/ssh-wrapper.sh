#!/bin/sh
# ssh for the runtests and graphqlserver images, installed as /usr/local/bin/ssh
# ahead of /usr/bin/ssh on PATH: it is what git and poetry's dulwich run to
# fetch a `git+ssh://` dependency. Movable with the images, like venv-cache.sh.
#
# The broker runs these images as the host uid, which has no passwd entry
# here, and ssh exits "No user exists for uid N" without one -- so a temporary
# entry is supplied through nss_wrapper, for ssh alone. Nobody is there to
# answer a prompt: BatchMode fails instead of hanging, and an unknown host key
# is trusted on first use. With the venv cache mounted the learned keys are
# kept there, so a host key that changes later fails every following run.
#
# The key itself comes from the ssh-agent socket the broker mounts
# ($SSH_AUTH_SOCK); without one, authentication fails.
set -eu

if mountpoint -q /cache; then
    known_hosts=/cache/ssh/known_hosts
else
    known_hosts="$HOME/.ssh/known_hosts"
fi
mkdir -p "$(dirname "$known_hosts")"

if ! getent passwd "$(id -u)" >/dev/null; then
    passwd=$(mktemp)
    cat /etc/passwd > "$passwd"
    printf '%s:x:%s:%s::%s:/bin/sh\n' "${USER:-runner}" "$(id -u)" "$(id -g)" "$HOME" >> "$passwd"
    export LD_PRELOAD=libnss_wrapper.so NSS_WRAPPER_PASSWD="$passwd" NSS_WRAPPER_GROUP=/etc/group
fi

exec /usr/bin/ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="$known_hosts" "$@"
