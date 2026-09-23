#!/bin/sh
. /www/cgi-bin/lib/pisowifi-common.sh
pisowifi_bootstrap

lock_state
state_load_all
served_count="$(served_clients_count)"
unlock_state

diff=$(( $(now) - ST_ESP_LAST_SEEN ))
if [ "$diff" -lt 30 ]; then esp_status="online"; else esp_status="offline"; fi

devices="["
first=1
active=0
for f in "$SESS_DIR"/*.json; do
  [ -f "$f" ] || continue
  mac="$(jsonfilter -i "$f" -e '@.mac' 2>/dev/null)"
  [ -z "$mac" ] && continue
  paused="$(jsonfilter -i "$f" -e '@.paused' 2>/dev/null)"
  total_paid="$(jsonfilter -i "$f" -e '@.total_paid' 2>/dev/null)"
  [ -z "$total_paid" ] && total_paid=0
  if [ "$paused" = "1" ]; then
    remaining="$(jsonfilter -i "$f" -e '@.paused_remaining' 2>/dev/null)"
  else
    expires_at="$(jsonfilter -i "$f" -e '@.expires_at' 2>/dev/null)"
    [ -z "$expires_at" ] && expires_at=0
    remaining=$(( expires_at - $(now) ))
    [ "$remaining" -lt 0 ] && remaining=0
  fi
  if [ "$remaining" -gt 0 ] 2>/dev/null; then
    [ "$first" = "1" ] || devices="$devices,"
    devices="$devices{\"mac\":\"$mac\",\"seconds_remaining\":$remaining,\"total_paid\":$total_paid}"
    first=0
    active=$((active + 1))
  fi
done
devices="$devices]"

http_json "200 OK" "{\"status\":\"ok\",\"total_sales\":$ST_TOTAL,\"daily_sales\":$ST_DAILY,\"monthly_sales\":$ST_MONTHLY,\"active_users\":$active,\"total_clients_served\":$served_count,\"esp8266_status\":\"$esp_status\",\"devices\":$devices}"
