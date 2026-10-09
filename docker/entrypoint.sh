#!/bin/sh
set -e
mkdir -p /data
chown -R pwuser:pwuser /data
# Chromium leaves a lock behind after an unclean stop; it would refuse to reuse the profile.
rm -f /data/browser-profile/SingletonLock /data/browser-profile/SingletonSocket /data/browser-profile/SingletonCookie
# Route PROXY_HOSTS (e.g. ha.example.com) through the reverse proxy container PROXY_VIA on a shared network.
if [ -n "$PROXY_VIA" ] && [ -n "$PROXY_HOSTS" ]; then
  ip=$(getent hosts "$PROXY_VIA" | awk '{print $1; exit}')
  if [ -n "$ip" ]; then
    for h in $PROXY_HOSTS; do
      grep -v "[[:space:]]$h\$" /etc/hosts > /tmp/hosts.new || true
      echo "$ip $h" >> /tmp/hosts.new
      cat /tmp/hosts.new > /etc/hosts
    done
    rm -f /tmp/hosts.new
    echo "entrypoint: $PROXY_HOSTS -> $PROXY_VIA ($ip)"
  else
    echo "entrypoint: $PROXY_VIA not resolvable, $PROXY_HOSTS use normal DNS"
  fi
fi
exec "$@"
