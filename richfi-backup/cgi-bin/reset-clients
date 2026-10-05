#!/bin/sh
. /www/cgi-bin/lib/pisowifi-common.sh
pisowifi_bootstrap

lock_state
state_load_all
ST_SERVED_JSON="[]"
save_state_now
unlock_state

http_json "200 OK" '{"status":"ok","total_clients_served":0,"message":"Total Clients Served has been reset."}'
