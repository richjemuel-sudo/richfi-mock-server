#!/bin/sh
# ==========================================================================
# pisowifi-common.sh
# Shared helpers for all RichFi PisoWiFi CGI scripts.
# Source this at the top of every .cgi: . /www/cgi-bin/lib/pisowifi-common.sh
# ==========================================================================

. /usr/share/libubox/jshn.sh

BASE_DIR="/etc/pisowifi"
CONFIG_FILE="$BASE_DIR/config.json"
CONFIG_LOCK="$BASE_DIR/config.lock"
STATE_FILE="$BASE_DIR/state.json"
STATE_LOCK="$BASE_DIR/state.lock"
SESS_DIR="$BASE_DIR/sessions"
LOCK_DIR="$BASE_DIR/locks"
ATTEMPT_DIR="$BASE_DIR/attempts"

mkdir -p "$BASE_DIR" "$SESS_DIR" "$LOCK_DIR" "$ATTEMPT_DIR"

PAYMENT_LOCK_TIMEOUT_S=30
MAX_FAILED_ATTEMPTS=6
BLOCK_DURATION_S=180

log() {
  logger -t pisowifi "$1" 2>/dev/null
}

now() { date +%s; }

atomic_write() {
  local target="$1"
  local dir tmp
  dir="$(dirname "$target")"
  tmp="$dir/.tmp.$$.$(basename "$target")"
  cat > "$tmp"
  mv "$tmp" "$target"
}

_flock_wait() {
  local fd="$1" timeout="$2" waited=0
  while ! flock -x -n "$fd" 2>/dev/null; do
    waited=$((waited + 1))
    [ "$waited" -ge "$((timeout * 10))" ] && return 1
    sleep 0.1
  done
  return 0
}

lock_config()   { exec 201>"$CONFIG_LOCK"; _flock_wait 201 5 || log "config lock timeout"; }
unlock_config() { flock -u 201 2>/dev/null; exec 201>&-; }

lock_state()    { exec 200>"$STATE_LOCK"; _flock_wait 200 5 || log "state lock timeout"; }
unlock_state()  { flock -u 200 2>/dev/null; exec 200>&-; }

lock_mac() {
  local mac="$1"
  local lf="$LOCK_DIR/$(mac_file_key "$mac").lock"
  exec 202>"$lf"
  _flock_wait 202 5 || log "mac lock timeout for $mac"
}
unlock_mac() { flock -u 202 2>/dev/null; exec 202>&-; }

http_json() {
  printf "Status: %s\r\n" "$1"
  printf "Content-Type: application/json\r\n"
  printf "Access-Control-Allow-Origin: *\r\n\r\n"
  printf "%s" "$2"
}

urldecode() {
  local s="${1//+/ }"
  printf '%b' "${s//%/\\x}"
}

parse_query() {
  local qs="$QUERY_STRING"
  local pair key val
  IFS='&'
  for pair in $qs; do
    key="${pair%%=*}"
    val="${pair#*=}"
    [ "$key" = "$pair" ] && val=""
    val="$(urldecode "$val")"
    key="$(echo "$key" | tr 'a-z' 'A-Z')"
    eval "Q_${key}=\"\$val\""
  done
  unset IFS
}

read_post_body() {
  BODY=""
  if [ -n "$CONTENT_LENGTH" ] && [ "$CONTENT_LENGTH" -gt 0 ] 2>/dev/null; then
    BODY="$(dd bs=1 count="$CONTENT_LENGTH" 2>/dev/null)"
  fi
}

body_field() {
  printf '%s' "$BODY" | jsonfilter -e "@.$1" 2>/dev/null
}

normalize_mac() {
  echo "$1" | tr 'a-z' 'A-Z' | sed 's/^ *//;s/ *$//'
}

mac_file_key() {
  echo "$1" | tr ':' '-'
}

session_file()  { echo "$SESS_DIR/$(mac_file_key "$1").json"; }
attempt_file()  { echo "$ATTEMPT_DIR/$(mac_file_key "$1").json"; }

DEFAULT_CONFIG='{
  "rates": [
    {"peso":1,"seconds":720,"label":"1 peso / 12 minutes","expire_seconds":0},
    {"peso":5,"seconds":7200,"label":"5 pesos / 2 hours","expire_seconds":0},
    {"peso":10,"seconds":18000,"label":"10 pesos / 5 hours","expire_seconds":0},
    {"peso":20,"seconds":43200,"label":"20 pesos / 12 hours","expire_seconds":0}
  ],
  "admin": {"username":"admin","password":"admin"},
  "data_limit": {"upload":"5Mbps","download":"5Mbps"}
}'

DEFAULT_STATE='{
  "total_sales": 0,
  "daily_sales": 0,
  "monthly_sales": 0,
  "served_clients": [],
  "waiting_mac": "",
  "waiting_since": 0,
  "payment_total": 0,
  "esp_last_seen": 0
}'

ensure_config_valid() {
  local ok=1
  if [ ! -f "$CONFIG_FILE" ]; then
    ok=0
  else
    local test_admin
    test_admin="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin.username' 2>/dev/null)"
    [ -z "$test_admin" ] && ok=0
    local test_rates
    test_rates="$(jsonfilter -i "$CONFIG_FILE" -e '@.rates[0].peso' 2>/dev/null)"
    [ -z "$test_rates" ] && ok=0
  fi
  if [ "$ok" = "0" ]; then
    log "config.json missing or corrupt - restoring defaults"
    lock_config
    printf '%s' "$DEFAULT_CONFIG" | atomic_write "$CONFIG_FILE"
    unlock_config
  fi
}

ensure_state_valid() {
  local ok=1
  if [ ! -f "$STATE_FILE" ]; then
    ok=0
  else
    local test_total
    test_total="$(jsonfilter -i "$STATE_FILE" -e '@.total_sales' 2>/dev/null)"
    case "$test_total" in
      ''|*[!0-9]*) ok=0 ;;
    esac
  fi
  if [ "$ok" = "0" ]; then
    log "state.json missing or corrupt - restoring defaults (sales counters reset)"
    lock_state
    printf '%s' "$DEFAULT_STATE" | atomic_write "$STATE_FILE"
    unlock_state
  fi
}

state_get() { jsonfilter -i "$STATE_FILE" -e "@.$1" 2>/dev/null; }

state_load_all() {
  ST_TOTAL="$(state_get total_sales)"; [ -z "$ST_TOTAL" ] && ST_TOTAL=0
  ST_DAILY="$(state_get daily_sales)"; [ -z "$ST_DAILY" ] && ST_DAILY=0
  ST_MONTHLY="$(state_get monthly_sales)"; [ -z "$ST_MONTHLY" ] && ST_MONTHLY=0
  ST_WAITING_MAC="$(state_get waiting_mac)"
  ST_WAITING_SINCE="$(state_get waiting_since)"; [ -z "$ST_WAITING_SINCE" ] && ST_WAITING_SINCE=0
  ST_PAYMENT_TOTAL="$(state_get payment_total)"; [ -z "$ST_PAYMENT_TOTAL" ] && ST_PAYMENT_TOTAL=0
  ST_ESP_LAST_SEEN="$(state_get esp_last_seen)"; [ -z "$ST_ESP_LAST_SEEN" ] && ST_ESP_LAST_SEEN=0
  ST_SERVED_JSON="$(sed -n 's/.*"served_clients": *\(\[[^]]*\]\).*/\1/p' "$STATE_FILE" | head -1)"
  [ -z "$ST_SERVED_JSON" ] && ST_SERVED_JSON="[]"
}

served_clients_add() {
  case "$ST_SERVED_JSON" in
    *"\"$1\""*) ;;
    "[]") ST_SERVED_JSON="[\"$1\"]" ;;
    *) ST_SERVED_JSON="${ST_SERVED_JSON%]},\"$1\"]" ;;
  esac
}

served_clients_count() {
  [ "$ST_SERVED_JSON" = "[]" ] && { echo 0; return; }
  echo "$ST_SERVED_JSON" | tr ',' '\n' | grep -c '"'
}

save_state_now() {
  atomic_write "$STATE_FILE" <<EOF
{
  "total_sales": $ST_TOTAL,
  "daily_sales": $ST_DAILY,
  "monthly_sales": $ST_MONTHLY,
  "served_clients": $ST_SERVED_JSON,
  "waiting_mac": "$ST_WAITING_MAC",
  "waiting_since": $ST_WAITING_SINCE,
  "payment_total": $ST_PAYMENT_TOTAL,
  "esp_last_seen": $ST_ESP_LAST_SEEN
}
EOF
}

ensure_session() {
  local mac="$1" f
  f="$(session_file "$mac")"
  if [ ! -f "$f" ]; then
    atomic_write "$f" <<EOF
{"mac":"$mac","expires_at":$(now),"paused":0,"paused_remaining":0,"total_paid":0,"last_coin_at":0,"session_start_at":0,"expire_seconds":0}
EOF
  fi
}

get_session_field() {
  local f
  f="$(session_file "$1")"
  [ -f "$f" ] || { echo ""; return; }
  jsonfilter -i "$f" -e "@.$2" 2>/dev/null
}

get_remaining_seconds() {
  local mac="$1" f paused expires_at rem cur
  f="$(session_file "$mac")"
  [ -f "$f" ] || { echo 0; return; }
  paused="$(jsonfilter -i "$f" -e '@.paused' 2>/dev/null)"
  if [ "$paused" = "1" ]; then
    jsonfilter -i "$f" -e '@.paused_remaining' 2>/dev/null
    return
  fi
  expires_at="$(jsonfilter -i "$f" -e '@.expires_at' 2>/dev/null)"
  [ -z "$expires_at" ] && expires_at=0
  cur="$(now)"
  rem=$((expires_at - cur))
  [ "$rem" -lt 0 ] && rem=0
  echo "$rem"
}

write_session() {
  local mac="$1" f
  f="$(session_file "$mac")"
  atomic_write "$f" <<EOF
{"mac":"$mac","expires_at":$2,"paused":$3,"paused_remaining":$4,"total_paid":$5,"last_coin_at":$6,"session_start_at":$7,"expire_seconds":$8}
EOF
}

enforce_session_expiration() {
  local mac="$1" start expire_s cur remaining total_paid
  EXPIRE_ENFORCED=0
  start="$(get_session_field "$mac" session_start_at)"; [ -z "$start" ] && start=0
  expire_s="$(get_session_field "$mac" expire_seconds)"; [ -z "$expire_s" ] && expire_s=0
  remaining="$(get_remaining_seconds "$mac")"

  if [ "$remaining" -gt 0 ] && [ "$expire_s" -gt 0 ] && [ "$start" -gt 0 ]; then
    cur="$(now)"
    if [ "$cur" -ge "$((start + expire_s))" ]; then
      total_paid="$(get_session_field "$mac" total_paid)"
      write_session "$mac" "$cur" 0 0 "$total_paid" "$(get_session_field "$mac" last_coin_at)" 0 0
      EXPIRE_ENFORCED=1
      remaining=0
      log "mac=$mac session expired at deadline (start=$start expire_seconds=$expire_s)"
    fi
  fi
  EXPIRE_REMAINING="$remaining"
}

get_attempt_field() {
  local f
  f="$(attempt_file "$1")"
  [ -f "$f" ] || { echo ""; return; }
  jsonfilter -i "$f" -e "@.$2" 2>/dev/null
}

write_attempt() {
  atomic_write "$(attempt_file "$1")" <<EOF
{"fail_count":$2,"blocked_until":$3}
EOF
}

clear_attempt() {
  rm -f "$(attempt_file "$1")" 2>/dev/null
}

get_rate_seconds_and_label() {
  RATE_SECONDS=""
  RATE_LABEL=""
  RATE_EXPIRE_SECONDS="0"
  json_load_file "$CONFIG_FILE"
  json_select rates
  local i=1
  while json_select "$i" 2>/dev/null; do
    json_get_var r_peso peso
    json_get_var r_seconds seconds
    json_get_var r_label label
    json_get_var r_expire expire_seconds 2>/dev/null
    json_select ..
    if [ "$r_peso" = "$1" ]; then
      RATE_SECONDS="$r_seconds"
      RATE_LABEL="$r_label"
      [ -n "$r_expire" ] && RATE_EXPIRE_SECONDS="$r_expire"
      break
    fi
    i=$((i+1))
  done
}

grant_access() {
  local target="$1" secs="$2"
  [ -n "$target" ] || return 1
  [ -n "$secs" ] && [ "$secs" -gt 0 ] 2>/dev/null || return 1

  if ndsctl auth "$target" >/dev/null 2>&1; then
    log "nds auth ok target=$target secs=$secs"
    return 0
  fi

  log "nds auth failed target=$target secs=$secs"
  return 1
}

deauth_access() {
  local target="$1"
  [ -n "$target" ] || return 1
  if ndsctl deauth "$target" >/dev/null 2>&1; then
    log "nds deauth ok target=$target"
    return 0
  fi
  return 1
}

grant_client_access() {
  local mac="$1" secs="$2" ip ok=0
  [ -n "$mac" ] || return 1
  [ -n "$secs" ] && [ "$secs" -gt 0 ] 2>/dev/null || return 1

  ip="$(mac_to_ip "$mac")"

  if [ -n "$ip" ]; then
    grant_access "$ip" "$secs" && ok=1
  fi

  if [ "$ok" != "1" ]; then
    grant_access "$(echo "$mac" | tr 'A-Z' 'a-z')" "$secs" && ok=1
  fi

  [ "$ok" = "1" ] && log "grant client ok mac=$mac ip=$ip secs=$secs" || log "grant client failed mac=$mac ip=$ip secs=$secs"
  [ "$ok" = "1" ]
}

deauth_client_access() {
    local mac="$1"
    local lower_mac

    [ -n "$mac" ] || return 1

    mac="$(normalize_mac "$mac")"
    lower_mac="$(echo "$mac" | tr 'A-Z' 'a-z')"

    [ -n "$lower_mac" ] || return 1

    log "DEAUTH START mac=$lower_mac"

    if ndsctl deauth "$lower_mac" >/dev/null 2>&1; then
        log "DEAUTH OK mac=$lower_mac"
        return 0
    fi

    log "DEAUTH FAILED mac=$lower_mac"
    return 1
}

schedule_deauth() {
  local mac="$1" secs="$2"
  [ -n "$mac" ] || return
  [ -n "$secs" ] && [ "$secs" -gt 0 ] 2>/dev/null || return
  (
    flock -u 200 2>/dev/null; exec 200>&- 2>/dev/null
    flock -u 201 2>/dev/null; exec 201>&- 2>/dev/null
    flock -u 202 2>/dev/null; exec 202>&- 2>/dev/null

    sleep "$secs"
    remaining="$(get_remaining_seconds "$mac")"
    paused="$(get_session_field "$mac" paused)"
    if [ "$paused" != "1" ] && [ "$remaining" -le 0 ] 2>/dev/null; then
      deauth_client_access "$mac"
    fi
  ) >/dev/null 2>&1 &
}

# --------------------------------------------------------------------------
# ndsctl json caching (2s TTL) - avoids every request spawning its own
# slow subprocess when several requests arrive close together.
# --------------------------------------------------------------------------
NDS_JSON_CACHE="/tmp/nds_json_cache"
NDS_JSON_CACHE_TTL=2

get_nds_json() {
  local ts_file="$NDS_JSON_CACHE.ts" last=0 cur
  [ -f "$ts_file" ] && last="$(cat "$ts_file" 2>/dev/null)"
  [ -z "$last" ] && last=0
  cur="$(now)"
  if [ "$((cur - last))" -ge "$NDS_JSON_CACHE_TTL" ]; then
    ndsctl json > "${NDS_JSON_CACHE}.tmp.$$" 2>/dev/null
    mv "${NDS_JSON_CACHE}.tmp.$$" "$NDS_JSON_CACHE" 2>/dev/null
    echo "$cur" > "$ts_file"
  fi
  cat "$NDS_JSON_CACHE" 2>/dev/null
}

mac_to_ip() {
  local mac="$1"

  mac="$(echo "$mac" | tr 'A-Z' 'a-z')"

  awk -v mac="$mac" '
    NR > 1 && tolower($4) == mac {
      print $1
      exit
    }
  ' /proc/net/arp 2>/dev/null
}

ip_to_mac() {
  local ip="$1"

  awk -v ip="$ip" '
    NR > 1 && $1 == ip {
      print tolower($4)
      exit
    }
  ' /proc/net/arp 2>/dev/null
}

# --------------------------------------------------------------------------
# Throttle for status action's self-heal re-auth - don't call ndsctl auth
# on every 3s poll, only once per REAUTH_THROTTLE_S window per mac.
# --------------------------------------------------------------------------
REAUTH_THROTTLE_S=20

should_reauth() {
  local mac="$1" f="/tmp/pisowifi_reauth_$(mac_file_key "$1")" last=0 cur
  [ -f "$f" ] && last="$(cat "$f" 2>/dev/null)"
  [ -z "$last" ] && last=0
  cur="$(now)"
  if [ "$((cur - last))" -ge "$REAUTH_THROTTLE_S" ]; then
    echo "$cur" > "$f"
    return 0
  fi
  return 1
}

release_stale_lock() {
  if [ -n "$ST_WAITING_MAC" ]; then
    local elapsed=$(( $(now) - ST_WAITING_SINCE ))
    if [ "$elapsed" -gt "$PAYMENT_LOCK_TIMEOUT_S" ]; then
      ST_WAITING_MAC=""
      ST_WAITING_SINCE=0
    fi
  fi
}

pisowifi_bootstrap() {
  ensure_config_valid
  ensure_state_valid
}

# --------------------------------------------------------------------------
# pause_all_active_sessions
# Called once at boot (from pisowifi-init) to freeze every running session
# so that a brownout / power cut does not drain the customer's remaining
# time while the router is off.
#
# For each session file where paused=0 and time still remains:
#   - compute remaining = expires_at - now()
#   - set paused=1, paused_remaining=remaining
#
# When the customer reconnects after the brownout they hit the splash page,
# see their frozen balance, and tap Resume to re-authorize.
# --------------------------------------------------------------------------
pause_all_active_sessions() {
  local f mac paused expires_at cur remaining
  local total_paid last_coin session_start expire_s

  cur="$(now)"

  for f in "$SESS_DIR"/*.json; do
    [ -f "$f" ] || continue

    paused="$(jsonfilter -i "$f" -e '@.paused' 2>/dev/null)"
    [ "$paused" = "1" ] && continue   # already paused - skip

    expires_at="$(jsonfilter -i "$f" -e '@.expires_at' 2>/dev/null)"
    [ -z "$expires_at" ] && expires_at=0

    remaining=$((expires_at - cur))
    [ "$remaining" -le 0 ] && continue  # already expired - skip

    mac="$(jsonfilter -i "$f" -e '@.mac' 2>/dev/null)"
    [ -z "$mac" ] && continue

    total_paid="$(jsonfilter -i "$f" -e '@.total_paid' 2>/dev/null)"
    [ -z "$total_paid" ] && total_paid=0

    last_coin="$(jsonfilter -i "$f" -e '@.last_coin_at' 2>/dev/null)"
    [ -z "$last_coin" ] && last_coin=0

    session_start="$(jsonfilter -i "$f" -e '@.session_start_at' 2>/dev/null)"
    [ -z "$session_start" ] && session_start=0

    expire_s="$(jsonfilter -i "$f" -e '@.expire_seconds' 2>/dev/null)"
    [ -z "$expire_s" ] && expire_s=0

    # Freeze the session - remaining time is now stored in paused_remaining
    write_session \
      "$mac" \
      "$expires_at" \
      1 \
      "$remaining" \
      "$total_paid" \
      "$last_coin" \
      "$session_start" \
      "$expire_s"

    log "boot: froze session mac=$mac remaining=${remaining}s (brownout protection)"
  done
}
# --------------------------------------------------------------------------
# Speed limiting helpers (tc HTB on br-lan)
#
# apply_speed_limit  <mac>  — call right after grant_client_access
# remove_speed_limit <mac>  — call right after deauth_client_access
# --------------------------------------------------------------------------

get_speed_limit_mbps() {
  local direction="$1"   # "upload" or "download"
  local raw val
  raw="$(jsonfilter -i "$CONFIG_FILE" -e "@.data_limit.$direction" 2>/dev/null)"
  # strip trailing "Mbps" / "mbps", e.g. "5Mbps" -> "5"
  val="${raw%[Mm]bps}"
  [ -z "$val" ] && val="5"
  echo "$val"
}

apply_speed_limit() {
  local mac="$1"
  local ip ul_mbps dl_mbps class_id

  ip="$(mac_to_ip "$mac")"
  [ -z "$ip" ] && { log "speed_limit: no IP for mac=$mac, skipping"; return 1; }

  ul_mbps="$(get_speed_limit_mbps upload)"
  dl_mbps="$(get_speed_limit_mbps download)"

  # Use last octet of IP as class ID (e.g. 192.168.1.45 -> 1:45)
  class_id="$(echo "$ip" | awk -F. '{print $4}')"
  [ -z "$class_id" ] && class_id=100

  # Tear down any previous class for this IP first (idempotent)
  tc filter del dev br-lan parent 1: handle "$class_id" fw 2>/dev/null || true
  tc class  del dev br-lan classid "1:$class_id"         2>/dev/null || true
  iptables -t mangle -D FORWARD -s "$ip" -j MARK --set-mark "$class_id" 2>/dev/null || true
  iptables -t mangle -D FORWARD -d "$ip" -j MARK --set-mark "$class_id" 2>/dev/null || true

  # Add HTB class for this client
  tc class add dev br-lan parent 1: classid "1:$class_id" htb \
    rate "${ul_mbps}mbit" burst 15k

  # Mark packets to/from this IP so tc can match them
  iptables -t mangle -A FORWARD -s "$ip" -j MARK --set-mark "$class_id"
  iptables -t mangle -D FORWARD -d "$ip" -j MARK --set-mark "$class_id" 2>/dev/null || true
  iptables -t mangle -A FORWARD -d "$ip" -j MARK --set-mark "$class_id"

  # Attach fw filter to direct marked packets into the class
  tc filter add dev br-lan parent 1: protocol ip handle "$class_id" fw \
    flowid "1:$class_id"

  log "speed_limit: applied ul=${ul_mbps}Mbps dl=${dl_mbps}Mbps mac=$mac ip=$ip class=1:$class_id"
}

remove_speed_limit() {
  local mac="$1"
  local ip class_id

  ip="$(mac_to_ip "$mac")"
  [ -z "$ip" ] && { log "speed_limit: no IP for mac=$mac on remove, skipping"; return; }

  class_id="$(echo "$ip" | awk -F. '{print $4}')"
  [ -z "$class_id" ] && return

  tc filter del dev br-lan parent 1: handle "$class_id" fw 2>/dev/null || true
  tc class  del dev br-lan classid "1:$class_id"         2>/dev/null || true
  iptables -t mangle -D FORWARD -s "$ip" -j MARK --set-mark "$class_id" 2>/dev/null || true
  iptables -t mangle -D FORWARD -d "$ip" -j MARK --set-mark "$class_id" 2>/dev/null || true

  log "speed_limit: removed for mac=$mac ip=$ip class=1:$class_id"
}