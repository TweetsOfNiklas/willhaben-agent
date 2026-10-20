#!/bin/sh
# VNC only listens on localhost; noVNC (websockify) is the only way in, and it is published on the LAN. Never without a
# password: without VNC_PASSWORD a random one is made once and kept in /data/vnc-password (readable on the host only).
PWFILE=/data/vnc-password
if [ -z "$VNC_PASSWORD" ]; then
  if [ ! -s "$PWFILE" ]; then
    (umask 077; head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-8 > "$PWFILE")
    echo "start-vnc: no VNC_PASSWORD set – generated one in data/vnc-password"
  fi
  VNC_PASSWORD=$(cat "$PWFILE")
fi
exec x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -passwd "$VNC_PASSWORD" -quiet
