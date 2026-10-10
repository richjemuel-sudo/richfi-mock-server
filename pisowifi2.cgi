#!/bin/sh
# ==========================================================================
# RichFi PisoWiFi CGI Backend
# ==========================================================================
#
# Main endpoints:
#
#   status
#   whoami
#   rates
#   update_rates
#   start_payment
#   waiting_status
#   coin
#   pause
#   resume
#   end_payment
#   kick
#   reset_stats
#   heartbeat
#   get_data_limit
#   set_data_limit
#   login
#   change_password
#
# ==========================================================================

. /www/cgi-bin/lib/pisowifi-common.sh

pisowifi_bootstrap

parse_query
read_post_body

ACTION="$Q_ACTION"

CLIENT_IP="$REMOTE_ADDR"

case "$ACTION" in

# ==========================================================================
# STATUS
# ==========================================================================
#
# IMPORTANT:
# This endpoint MUST remain read-only and fast.
#
# DO NOT call ndsctl auth/deauth here.
#
# ==========================================================================

status)

  mac="$(normalize_mac "$Q_MAC")"

  if [ -z "$mac" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"MAC address is required"}'

    exit 0
  fi

  lock_mac "$mac"

  ensure_session "$mac"

  enforce_session_expiration "$mac"

  remaining="$EXPIRE_REMAINING"

  forfeited="$EXPIRE_ENFORCED"

  paused="$(get_session_field "$mac" paused)"

  unlock_mac

  [ "$paused" = "1" ] && is_paused=true || is_paused=false

  if [ "$remaining" -gt 0 ]; then

    lock_state

    state_load_all

    ptotal=0

    if [ "$mac" = "$ST_WAITING_MAC" ]; then
      ptotal="$ST_PAYMENT_TOTAL"
    fi

    unlock_state

    http_json \
      "200 OK" \
      "{\"status\":\"authorized\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining,\"payment_total\":$ptotal,\"is_paused\":$is_paused}"

  else

    http_json \
      "200 OK" \
      "{\"status\":\"unauthorized\",\"mac\":\"$mac\",\"seconds_remaining\":0,\"payment_total\":0,\"is_paused\":$is_paused}"

  fi

  ;;

# ==========================================================================
# WHOAMI
# ==========================================================================

whoami)

  mac=""

  if [ -n "$CLIENT_IP" ]; then

    mac="$(
      awk -v ip="$CLIENT_IP" \
      '$1==ip{print $4; exit}' \
      /proc/net/arp
    )"

  fi

  if [ -z "$mac" ] ||
     [ "$mac" = "00:00:00:00:00:00" ]; then

    http_json \
      "200 OK" \
      '{"status":"error","message":"MAC not resolved"}'

    exit 0
  fi

  mac="$(normalize_mac "$mac")"

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"mac\":\"$mac\"}"

  ;;

# ==========================================================================
# RATES
# ==========================================================================

rates)

  rates_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.rates'
  )"

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"rates\":$rates_json}"

  ;;

# ==========================================================================
# UPDATE RATES
# ==========================================================================

update_rates)

  if [ -z "$BODY" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Rates array required"}'

    exit 0
  fi

  new_rates="["
  first=1
  i=0
  ok=1

  while :; do

    price="$(
      printf '%s' "$BODY" |
      jsonfilter -e "@[$i].price" 2>/dev/null
    )"

    [ -z "$price" ] && break

    minutes="$(
      printf '%s' "$BODY" |
      jsonfilter -e "@[$i].minutes" 2>/dev/null
    )"

    name="$(
      printf '%s' "$BODY" |
      jsonfilter -e "@[$i].name" 2>/dev/null
    )"

    expiration="$(
      printf '%s' "$BODY" |
      jsonfilter -e "@[$i].expiration" 2>/dev/null
    )"

    [ -z "$minutes" ] && ok=0

    secs="$(
      awk \
      "BEGIN{printf \"%d\", $minutes*60}" \
      2>/dev/null
    )"

    [ -z "$secs" ] && secs=0

    expire_secs=0

    case "$expiration" in

      ''|*[!0-9.]*)
        expire_secs=0
        ;;

      *)
        expire_secs="$(
          awk \
          "BEGIN{printf \"%d\", $expiration*60}" \
          2>/dev/null
        )"

        [ -z "$expire_secs" ] &&
          expire_secs=0

        ;;

    esac

    case "$price" in

      ''|*[!0-9]*)
        ok=0
        ;;

      *)
        [ "$price" -le 0 ] &&
          ok=0
        ;;

    esac

    case "$secs" in

      ''|*[!0-9]*)
        ok=0
        ;;

      *)
        [ "$secs" -le 0 ] &&
          ok=0
        ;;

    esac

    label="$name"

    [ -z "$label" ] &&
      label="$price peso / $minutes minutes"

    [ "$first" = "1" ] ||
      new_rates="$new_rates,"

    new_rates="$new_rates{\"peso\":$price,\"seconds\":$secs,\"label\":\"$label\",\"expire_seconds\":$expire_secs}"

    first=0

    i=$((i + 1))

  done

  new_rates="$new_rates]"

  if [ "$ok" = "0" ] ||
     [ "$first" = "1" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Invalid rate values"}'

    exit 0
  fi

  lock_config

  admin_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin'
  )"

  dl_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.data_limit'
  )"

  atomic_write "$CONFIG_FILE" <<EOF
{"rates":$new_rates,"admin":$admin_json,"data_limit":$dl_json}
EOF

  unlock_config

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"rates\":$new_rates}"

  ;;

# ==========================================================================
# START PAYMENT
# ==========================================================================

start_payment)

  mac="$(normalize_mac "$Q_MAC")"

  if [ -z "$mac" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"MAC address is required"}'

    exit 0
  fi

  lock_mac "$mac"

  blocked_until="$(
    get_attempt_field \
      "$mac" \
      blocked_until
  )"

  unlock_mac

  [ -z "$blocked_until" ] &&
    blocked_until=0

  cur="$(now)"

  if [ "$blocked_until" -gt "$cur" ]; then

    retry=$((blocked_until - cur))

    http_json \
      "429 Too Many Requests" \
      "{\"status\":\"blocked\",\"message\":\"Too many attempts without inserting a coin. Please wait before trying again.\",\"retry_after_seconds\":$retry}"

    exit 0
  fi

  lock_state

  state_load_all

  release_stale_lock

  if [ -n "$ST_WAITING_MAC" ] &&
     [ "$ST_WAITING_MAC" != "$mac" ]; then

    save_state_now

    unlock_state

    http_json \
      "409 Conflict" \
      '{"status":"busy","message":"Another customer is currently paying. Please wait."}'

    exit 0
  fi

  ST_WAITING_MAC="$mac"

  ST_WAITING_SINCE="$cur"

  ST_PAYMENT_TOTAL=0

  save_state_now

  unlock_state

  http_json \
    "200 OK" \
    "{\"status\":\"waiting\",\"mac\":\"$mac\",\"payment_total\":0,\"message\":\"Waiting for coin\"}"

  ;;

# ==========================================================================
# WAITING STATUS
# ==========================================================================
#
# Used ONLY by ESP32.
#
# This endpoint does NOT call ndsctl.
# This keeps polling fast.
#
# ==========================================================================

waiting_status)

  lock_state

  state_load_all

  cur="$(now)"

  waiting=false
  waiting_mac=""
  waiting_since=0
  payment_total=0
  remaining_window=0
  stale=0

  if [ -n "$ST_WAITING_MAC" ]; then

    elapsed=$((cur - ST_WAITING_SINCE))

    if [ "$elapsed" -ge "$PAYMENT_LOCK_TIMEOUT_S" ]; then

      ST_WAITING_MAC=""
      ST_WAITING_SINCE=0
      ST_PAYMENT_TOTAL=0

      stale=1

    else

      waiting=true

      waiting_mac="$ST_WAITING_MAC"

      waiting_since="$ST_WAITING_SINCE"

      payment_total="$ST_PAYMENT_TOTAL"

      remaining_window=$(
        expr "$PAYMENT_LOCK_TIMEOUT_S" - "$elapsed"
      )

      [ "$remaining_window" -lt 0 ] &&
        remaining_window=0

    fi

  fi

  if [ "$stale" = "1" ]; then
    save_state_now
  fi

  unlock_state

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"waiting\":$waiting,\"mac\":\"$waiting_mac\",\"waiting_since\":$waiting_since,\"payment_total\":$payment_total,\"remaining_seconds\":$remaining_window}"

  ;;

# ==========================================================================
# COIN
# ==========================================================================
#
# ESP32 sends:
#
#   action=coin&peso=1
#
# The backend identifies the customer using ST_WAITING_MAC.
#
# ==========================================================================

coin)

  peso="$Q_PESO"

  get_rate_seconds_and_label "$peso"

  if [ -z "$RATE_SECONDS" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Invalid coin value"}'

    exit 0
  fi

  lock_state

  state_load_all

  release_stale_lock

  ST_ESP_LAST_SEEN="$(now)"

  if [ -z "$ST_WAITING_MAC" ]; then

    save_state_now

    unlock_state

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"No customer is waiting for payment"}'

    exit 0
  fi

  mac="$ST_WAITING_MAC"

  ST_WAITING_SINCE="$(now)"

  lock_mac "$mac"

  ensure_session "$mac"

  enforce_session_expiration "$mac"

  remaining="$EXPIRE_REMAINING"

  new_remaining=$(
    expr "$remaining" + "$RATE_SECONDS"
  )

  paused="$(
    get_session_field \
      "$mac" \
      paused
  )"

  total_paid="$(
    get_session_field \
      "$mac" \
      total_paid
  )"

  [ -z "$total_paid" ] &&
    total_paid=0

  new_total_paid=$(
    expr "$total_paid" + "$peso"
  )

  cur="$(now)"

  if [ "$remaining" -le 0 ]; then

    session_start_at="$cur"

    session_expire_seconds="$RATE_EXPIRE_SECONDS"

  else

    session_start_at="$(
      get_session_field \
        "$mac" \
        session_start_at
    )"

    [ -z "$session_start_at" ] &&
      session_start_at=0

    session_expire_seconds="$(
      get_session_field \
        "$mac" \
        expire_seconds
    )"

    [ -z "$session_expire_seconds" ] &&
      session_expire_seconds=0

  fi

  if [ "$paused" = "1" ]; then

    write_session \
      "$mac" \
      "$(get_session_field "$mac" expires_at)" \
      1 \
      "$new_remaining" \
      "$new_total_paid" \
      "$cur" \
      "$session_start_at" \
      "$session_expire_seconds"

  else

    new_expires=$(
      expr "$cur" + "$new_remaining"
    )

    write_session \
      "$mac" \
      "$new_expires" \
      0 \
      0 \
      "$new_total_paid" \
      "$cur" \
      "$session_start_at" \
      "$session_expire_seconds"

   
   #--------------------------------------------------------------------
   # Resolve IP from customer's MAC FIRST.
   # This fixed the previous problem where stale CLIENT_IP
   # 192.168.1.110 was used instead of 192.168.1.128.
   #--------------------------------------------------------------------
  

    ip="$(mac_to_ip "$mac")"

    [ -z "$ip" ] &&
      ip="$CLIENT_IP"

    grant_ip="$ip"

    grant_secs="$new_remaining"

  fi

  unlock_mac

  ST_PAYMENT_TOTAL=$(
    expr "$ST_PAYMENT_TOTAL" + "$peso"
  )

  ST_TOTAL=$(
    expr "$ST_TOTAL" + "$peso"
  )

  ST_DAILY=$(
    expr "$ST_DAILY" + "$peso"
  )

  ST_MONTHLY=$(
    expr "$ST_MONTHLY" + "$peso"
  )

  served_clients_add "$mac"

  save_state_now

  payment_total_out="$ST_PAYMENT_TOTAL"

  unlock_state


  #----------------------------------------------------------------------
  # Authorize client
  #----------------------------------------------------------------------


  grant_access \
    "$grant_ip" \
    "$grant_secs"

  http_json \
    "200 OK" \
    "{\"status\":\"authorized\",\"mac\":\"$mac\",\"peso\":$peso,\"seconds_added\":$RATE_SECONDS,\"seconds_remaining\":$new_remaining,\"payment_total\":$payment_total_out}"

  ;;

# ==========================================================================
# PAUSE
# ==========================================================================

pause)

  mac="$(normalize_mac "$Q_MAC")"

  lock_mac "$mac"

  f="$(session_file "$mac")"

  if [ ! -f "$f" ]; then

    unlock_mac

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"No session found"}'

    exit 0
  fi

  enforce_session_expiration "$mac"

  paused="$(get_session_field "$mac" paused)"

  if [ "$EXPIRE_ENFORCED" = "1" ]; then

    ip="$(mac_to_ip "$mac")"

    deauth_access "$ip"

    remaining=0

  elif [ "$paused" != "1" ]; then

    remaining="$EXPIRE_REMAINING"

    total_paid="$(
      get_session_field \
        "$mac" \
        total_paid
    )"

    write_session \
      "$mac" \
      "$(now)" \
      1 \
      "$remaining" \
      "$total_paid" \
      "$(get_session_field "$mac" last_coin_at)" \
      "$(get_session_field "$mac" session_start_at)" \
      "$(get_session_field "$mac" expire_seconds)"

    ip="$(mac_to_ip "$mac")"

    deauth_access "$ip"

  else

    remaining="$(
      get_session_field \
        "$mac" \
        paused_remaining
    )"

  fi

  unlock_mac

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining}"

  ;;

# ==========================================================================
# RESUME
# ==========================================================================

resume)

  mac="$(normalize_mac "$Q_MAC")"

  lock_mac "$mac"

  f="$(session_file "$mac")"

  if [ ! -f "$f" ]; then

    unlock_mac

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"No session found"}'

    exit 0
  fi

  enforce_session_expiration "$mac"

  paused="$(get_session_field "$mac" paused)"

  total_paid="$(
    get_session_field \
      "$mac" \
      total_paid
  )"

  if [ "$EXPIRE_ENFORCED" = "1" ]; then

    remaining=0

  elif [ "$paused" = "1" ]; then

    remaining="$EXPIRE_REMAINING"

    new_expires=$(
      expr "$(now)" + "$remaining"
    )

    write_session \
      "$mac" \
      "$new_expires" \
      0 \
      0 \
      "$total_paid" \
      "$(get_session_field "$mac" last_coin_at)" \
      "$(get_session_field "$mac" session_start_at)" \
      "$(get_session_field "$mac" expire_seconds)"

    #----------------------------------------------------------------------
    # Resolve MAC -> IP first
    #----------------------------------------------------------------------

    ip="$(mac_to_ip "$mac")"

    [ -z "$ip" ] &&
      ip="$CLIENT_IP"

    grant_access \
      "$ip" \
      "$remaining"

  else

    remaining="$(
      get_remaining_seconds \
        "$mac"
    )"

  fi

  unlock_mac

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining}"

  ;;

# ==========================================================================
# END PAYMENT
# ==========================================================================

end_payment)

  mac="$(normalize_mac "$Q_MAC")"

  lock_state

  state_load_all

  was_waiting=0

  [ "$ST_WAITING_MAC" = "$mac" ] &&
    was_waiting=1

  payment_total_snapshot="$ST_PAYMENT_TOTAL"

  if [ "$was_waiting" = "1" ]; then

    ST_WAITING_MAC=""

    ST_WAITING_SINCE=0

    ST_PAYMENT_TOTAL=0

    save_state_now

  fi

  unlock_state

  if [ "$was_waiting" = "1" ]; then

    lock_mac "$mac"

    if [ "$payment_total_snapshot" = "0" ]; then

      fail_count="$(
        get_attempt_field \
          "$mac" \
          fail_count
      )"

      [ -z "$fail_count" ] &&
        fail_count=0

      fail_count=$(
        expr "$fail_count" + 1
      )

      blocked_until=0

      if [ "$fail_count" -ge "$MAX_FAILED_ATTEMPTS" ]; then

        blocked_until=$(
          expr "$(now)" + "$BLOCK_DURATION_S"
        )

        fail_count=0

      fi

      write_attempt \
        "$mac" \
        "$fail_count" \
        "$blocked_until"

    else

      clear_attempt "$mac"

    fi

    unlock_mac

  fi

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"mac\":\"$mac\"}"

  ;;

# ==========================================================================
# KICK
# ==========================================================================

kick)

  mac="$(normalize_mac "$Q_MAC")"

  lock_mac "$mac"

  f="$(session_file "$mac")"

  if [ -f "$f" ]; then

    total_paid="$(
      get_session_field \
        "$mac" \
        total_paid
    )"

    write_session \
      "$mac" \
      "$(now)" \
      0 \
      0 \
      "$total_paid" \
      "$(get_session_field "$mac" last_coin_at)" \
      "$(now)" \
      0

    ip="$(mac_to_ip "$mac")"

    deauth_access "$ip"

  fi

  unlock_mac

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"mac\":\"$mac\"}"

  ;;

# ==========================================================================
# RESET STATS
# ==========================================================================

reset_stats)

  lock_state

  state_load_all

  case "$Q_WHICH" in

    total)
      ST_TOTAL=0
      ;;

    daily)
      ST_DAILY=0
      ;;

    monthly)
      ST_MONTHLY=0
      ;;

    *)

      unlock_state

      http_json \
        "400 Bad Request" \
        '{"status":"error","message":"Invalid \"which\" parameter"}'

      exit 0

      ;;

  esac

  save_state_now

  unlock_state

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"which\":\"$Q_WHICH\"}"

  ;;

# ==========================================================================
# HEARTBEAT
# ==========================================================================

heartbeat)

  lock_state

  state_load_all

  ST_ESP_LAST_SEEN="$(now)"

  save_state_now

  unlock_state

  http_json \
    "200 OK" \
    '{"status":"ok"}'

  ;;

# ==========================================================================
# GET DATA LIMIT
# ==========================================================================

get_data_limit)

  dl_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.data_limit'
  )"

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"data_limit\":$dl_json}"

  ;;

# ==========================================================================
# SET DATA LIMIT
# ==========================================================================

set_data_limit)

  upload="$Q_UPLOAD"

  download="$Q_DOWNLOAD"

  if [ -z "$upload" ] ||
     [ -z "$download" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"upload and download are required"}'

    exit 0
  fi

  lock_config

  rates_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.rates'
  )"

  admin_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin'
  )"

  atomic_write "$CONFIG_FILE" <<EOF
{"rates":$rates_json,"admin":$admin_json,"data_limit":{"upload":"$upload","download":"$download"}}
EOF

  unlock_config

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"data_limit\":{\"upload\":\"$upload\",\"download\":\"$download\"}}"

  ;;

# ==========================================================================
# LOGIN
# ==========================================================================

login)

  username="$(body_field username)"

  password="$(body_field password)"

  admin_user="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin.username'
  )"

  admin_pass="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin.password'
  )"

  if [ -z "$username" ] ||
     [ -z "$password" ]; then

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Username and password are required"}'

    exit 0
  fi

  if [ "$username" = "$admin_user" ] &&
     [ "$password" = "$admin_pass" ]; then

    http_json \
      "200 OK" \
      "{\"status\":\"ok\",\"username\":\"$username\"}"

  else

    http_json \
      "401 Unauthorized" \
      '{"status":"error","message":"Invalid username or password"}'

  fi

  ;;

# ==========================================================================
# CHANGE PASSWORD
# ==========================================================================

change_password)

  username="$(body_field username)"

  current_password="$(
    body_field current_password
  )"

  new_password="$(
    body_field new_password
  )"

  lock_config

  admin_user="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin.username'
  )"

  admin_pass="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.admin.password'
  )"

  if [ -z "$username" ] ||
     [ -z "$current_password" ] ||
     [ -z "$new_password" ]; then

    unlock_config

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"All fields are required"}'

    exit 0
  fi

  if [ "$username" != "$admin_user" ] ||
     [ "$current_password" != "$admin_pass" ]; then

    unlock_config

    http_json \
      "401 Unauthorized" \
      '{"status":"error","message":"Current password is incorrect"}'

    exit 0
  fi

  if [ "${#new_password}" -lt 4 ]; then

    unlock_config

    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"New password must be at least 4 characters"}'

    exit 0
  fi

  rates_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.rates'
  )"

  dl_json="$(
    jsonfilter \
      -i "$CONFIG_FILE" \
      -e '@.data_limit'
  )"

  atomic_write "$CONFIG_FILE" <<EOF
{"rates":$rates_json,"admin":{"username":"$username","password":"$new_password"},"data_limit":$dl_json}
EOF

  unlock_config

  http_json \
    "200 OK" \
    '{"status":"ok","message":"Password updated"}'

  ;;

# ==========================================================================
# GENERATE VOUCHERS
# ==========================================================================
#
#  POST body (JSON):
#    { "prefix":"RF", "name":"1 Hour", "price":5,
#      "seconds":3600, "qty":12, "save_sales":true,
#      "codes":["RF1234","RF5678",...] }
#
# Saves each code as a flat JSON file under $VOUCHER_DIR/<CODE>.json
# ==========================================================================

generate_vouchers)

  VOUCHER_DIR="$BASE_DIR/vouchers"
  mkdir -p "$VOUCHER_DIR"

  if [ -z "$BODY" ]; then
    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Request body is required"}'
    exit 0
  fi

  vprefix="$(printf '%s' "$BODY"  | jsonfilter -e '@.prefix'     2>/dev/null)"
  vname="$(printf '%s' "$BODY"    | jsonfilter -e '@.name'       2>/dev/null)"
  vprice="$(printf '%s' "$BODY"   | jsonfilter -e '@.price'      2>/dev/null)"
  vseconds="$(printf '%s' "$BODY" | jsonfilter -e '@.seconds'    2>/dev/null)"
  vqty="$(printf '%s' "$BODY"     | jsonfilter -e '@.qty'        2>/dev/null)"
  vsave="$(printf '%s' "$BODY"    | jsonfilter -e '@.save_sales' 2>/dev/null)"

  # Validate required fields
  if [ -z "$vname" ] || [ -z "$vseconds" ] || [ -z "$vqty" ]; then
    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Missing required fields: name, seconds, qty"}'
    exit 0
  fi

  [ -z "$vprice" ]  && vprice=0
  [ -z "$vprefix" ] && vprefix="RF"
  [ "$vsave" = "true" ] && vsave=1 || vsave=0

  saved=0
  skipped=0
  cur="$(now)"

  i=0
  while [ "$i" -lt "$vqty" ]; do
    code="$(printf '%s' "$BODY" | jsonfilter -e "@.codes[$i]" 2>/dev/null)"
    i=$((i + 1))
    [ -z "$code" ] && continue

    vfile="$VOUCHER_DIR/${code}.json"

    # Skip if code file already exists (safety — no overwrite)
    if [ -f "$vfile" ]; then
      skipped=$((skipped + 1))
      continue
    fi

    atomic_write "$vfile" <<EOF
{"code":"$code","name":"$vname","prefix":"$vprefix","price":$vprice,"seconds":$vseconds,"save_sales":$vsave,"used":0,"used_by":"","used_at":0,"created_at":$cur}
EOF
    saved=$((saved + 1))
  done

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"saved\":$saved,\"skipped\":$skipped,\"message\":\"$saved voucher(s) saved\"}"

  ;;

# ==========================================================================
# REDEEM VOUCHER
# ==========================================================================
#
#  GET ?action=redeem_voucher&mac=XX:XX:XX:XX:XX:XX&code=RF1234
#
#  Responses:
#    {"status":"ok",    "seconds_added":3600, "message":"..."}
#    {"status":"error", "message":"Voucher code already in use"}
#    {"status":"error", "message":"Invalid voucher code"}
# ==========================================================================

redeem_voucher)

  VOUCHER_DIR="$BASE_DIR/vouchers"

  mac="$(normalize_mac "$Q_MAC")"
  code="$(echo "$Q_CODE" | tr 'a-z' 'A-Z' | sed 's/^ *//;s/ *$//')"

  if [ -z "$mac" ]; then
    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"MAC address is required"}'
    exit 0
  fi

  if [ -z "$code" ]; then
    http_json \
      "400 Bad Request" \
      '{"status":"error","message":"Voucher code is required"}'
    exit 0
  fi

  vfile="$VOUCHER_DIR/${code}.json"

  # Code does not exist
  if [ ! -f "$vfile" ]; then
    http_json \
      "200 OK" \
      '{"status":"error","message":"Invalid voucher code"}'
    exit 0
  fi

  # Use a per-voucher lock file to prevent race conditions
  vlock="$VOUCHER_DIR/${code}.lock"
  exec 203>"$vlock"
  _flock_wait 203 5 || {
    http_json \
      "200 OK" \
      '{"status":"error","message":"Voucher temporarily locked. Please try again."}'
    exit 0
  }

  # Read voucher fields
  vused="$(jsonfilter    -i "$vfile" -e '@.used'       2>/dev/null)"
  vused_by="$(jsonfilter -i "$vfile" -e '@.used_by'    2>/dev/null)"
  vseconds="$(jsonfilter -i "$vfile" -e '@.seconds'    2>/dev/null)"
  vname="$(jsonfilter    -i "$vfile" -e '@.name'       2>/dev/null)"
  vprice="$(jsonfilter   -i "$vfile" -e '@.price'      2>/dev/null)"
  vprefix="$(jsonfilter  -i "$vfile" -e '@.prefix'     2>/dev/null)"
  vsave="$(jsonfilter    -i "$vfile" -e '@.save_sales' 2>/dev/null)"
  vcreated="$(jsonfilter -i "$vfile" -e '@.created_at' 2>/dev/null)"

  [ -z "$vseconds" ] && vseconds=0
  [ -z "$vprice" ]   && vprice=0
  [ -z "$vsave" ]    && vsave=0

  # Already used — check if same MAC or different
  if [ "$vused" = "1" ]; then
    if [ "$vused_by" = "$mac" ]; then
      flock -u 203 2>/dev/null; exec 203>&-
      http_json \
        "200 OK" \
        '{"status":"error","message":"Voucher code already in use"}'
    else
      flock -u 203 2>/dev/null; exec 203>&-
      http_json \
        "200 OK" \
        '{"status":"error","message":"Voucher code already in use"}'
    fi
    exit 0
  fi

  # Mark voucher as used
  cur="$(now)"

  atomic_write "$vfile" <<EOF
{"code":"$code","name":"$vname","prefix":"$vprefix","price":$vprice,"seconds":$vseconds,"save_sales":$vsave,"used":1,"used_by":"$mac","used_at":$cur,"created_at":$vcreated}
EOF

  flock -u 203 2>/dev/null
  exec 203>&-

  # Add time to this MAC's session
  lock_mac "$mac"

  ensure_session "$mac"
  enforce_session_expiration "$mac"

  remaining="$EXPIRE_REMAINING"
  new_remaining=$(expr "$remaining" + "$vseconds")

  paused="$(get_session_field "$mac" paused)"
  total_paid="$(get_session_field "$mac" total_paid)"
  [ -z "$total_paid" ] && total_paid=0

  vprice_int=$(printf '%.0f' "$vprice" 2>/dev/null || echo "${vprice%%.*}")
  [ -z "$vprice_int" ] && vprice_int=0

  new_total_paid=$(expr "$total_paid" + "$vprice_int")
  session_start_at="$(get_session_field "$mac" session_start_at)"
  [ -z "$session_start_at" ] && session_start_at=0
  session_expire_seconds="$(get_session_field "$mac" expire_seconds)"
  [ -z "$session_expire_seconds" ] && session_expire_seconds=0

  if [ "$paused" = "1" ]; then
    write_session \
      "$mac" \
      "$(get_session_field "$mac" expires_at)" \
      1 \
      "$new_remaining" \
      "$new_total_paid" \
      "$cur" \
      "$session_start_at" \
      "$session_expire_seconds"
  else
    new_expires=$(expr "$cur" + "$new_remaining")
    write_session \
      "$mac" \
      "$new_expires" \
      0 \
      0 \
      "$new_total_paid" \
      "$cur" \
      "$session_start_at" \
      "$session_expire_seconds"

    ip="$(mac_to_ip "$mac")"
    [ -z "$ip" ] && ip="$CLIENT_IP"
    grant_access "$ip" "$new_remaining"
  fi

  unlock_mac

  # Update sales stats if save_sales is enabled
  # vsave is stored as 1/0 integer; vprice truncated to int for expr
  vprice_int=$(printf '%.0f' "$vprice" 2>/dev/null || echo "${vprice%%.*}")
  [ -z "$vprice_int" ] && vprice_int=0

  if [ "$vsave" = "1" ] && [ "$vprice_int" -gt 0 ] 2>/dev/null; then
    lock_state
    state_load_all
    ST_TOTAL=$(expr "$ST_TOTAL" + "$vprice_int")
    ST_DAILY=$(expr "$ST_DAILY" + "$vprice_int")
    ST_MONTHLY=$(expr "$ST_MONTHLY" + "$vprice_int")
    served_clients_add "$mac"
    save_state_now
    unlock_state
  fi

  http_json \
    "200 OK" \
    "{\"status\":\"ok\",\"seconds_added\":$vseconds,\"seconds_remaining\":$new_remaining,\"message\":\"$vname voucher redeemed! Time added.\"}"

  ;;

# ==========================================================================
# UNKNOWN ACTION
# ==========================================================================

*)

  http_json \
    "400 Bad Request" \
    '{"status":"error","message":"Unknown action"}'

  ;;

esac