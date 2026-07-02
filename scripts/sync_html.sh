#!/bin/bash
while true; do
  inotifywait -e close_write,move,create,delete /root/TiktokLive/frontend/app.html
  cp /root/TiktokLive/frontend/app.html /var/www/tiktok-live/app.html
  echo "[$(date)] Sincronizado app.html a /var/www/tiktok-live/"
done
