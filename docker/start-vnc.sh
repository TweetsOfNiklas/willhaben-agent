#!/bin/sh
# VNC only listens on localhost; noVNC (websockify) is the only way in. Set VNC_PASSWORD!
if [ -n "$VNC_PASSWORD" ]; then
  exec x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -passwd "$VNC_PASSWORD" -quiet
else
  exec x11vnc -display :99 -forever -shared -localhost -rfbport 5900 -nopw -quiet
fi
