#!/bin/sh
. /www/cgi-bin/lib/pisowifi-common.sh
pisowifi_bootstrap

esp_last_seen="$(state_get esp_last_seen)"
[ -z "$esp_last_seen" ] && esp_last_seen=0
diff=$(( $(now) - esp_last_seen ))
if [ "$diff" -lt 30 ]; then esp_status="online"; else esp_status="offline"; fi

ts="$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")"

http_json "200 OK" "{\"status\":\"ok\",\"server\":\"online\",\"esp8266_status\":\"$esp_status\",\"timestamp\":\"$ts\"}"
