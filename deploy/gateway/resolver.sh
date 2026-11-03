#!/bin/sh
# Chooses the DNS servers nginx uses to look up egress destinations, written to an nginx include file.
#
# It can't be Docker's own DNS (127.0.0.11): on the app's network every allowed hostname is an alias of this
# gateway, so Docker would answer "this container" and nginx would connect to itself. In order of preference:
#   1. PRINTPOOL_DNS, from [gateway] dns in printpool.toml
#   2. the upstream servers Docker itself forwards to (recorded in /etc/resolv.conf as "ExtServers")
#   3. public resolvers, as a last resort
set -eu

out=/etc/nginx/printpool-resolver.conf
servers=${PRINTPOOL_DNS:-}
source="printpool.toml"

if [ -z "$servers" ]; then
    # e.g. "# ExtServers: [host(192.168.1.1) host(8.8.8.8)]"; loopback servers aren't reachable from here.
    servers=$(sed -n 's/^# ExtServers: \[\(.*\)\]/\1/p' /etc/resolv.conf \
        | tr ' ' '\n' | sed -e 's/^host(//' -e 's/)$//' | grep -Ev '^(127\.|::1$|$)' | tr '\n' ' ' || true)
    source="Docker's upstream DNS"
fi
if [ -z "$(echo "$servers" | tr -d ' ')" ]; then
    servers="1.1.1.1 8.8.8.8"
    source="public fallback"
    echo "$0: warning: couldn't find this machine's DNS servers; using $servers. Set [gateway] dns in printpool.toml if lookups fail." >&2
fi

line="resolver"
for s in $servers; do
    case $s in *:*) line="$line [$s]" ;; *) line="$line $s" ;; esac
done
echo "$line valid=60s ipv6=off;" > "$out"
echo "$0: egress DNS ($source): $servers"
