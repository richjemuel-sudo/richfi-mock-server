#!/bin/sh
# ==========================================================================
# RichFi PisoWiFi CGI backend
# http://<router-ip>/cgi-bin/pisowifi
# ==========================================================================

. /www/cgi-bin/lib/pisowifi-common.sh

pisowifi_bootstrap
parse_query
read_post_body

ACTION="$Q_ACTION"
CLIENT_IP="$REMOTE_ADDR"
COIN_ENABLED_FILE="/tmp/richfi_coin_enabled"

case "$ACTION" in

  status)
    mac="$(normalize_mac "$Q_MAC")"
    if [ -z "$mac" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"MAC address is required"}'
      exit 0
    fi

    lock_mac "$mac"
    ensure_session "$mac"
    enforce_session_expiration "$mac"
    remaining="$EXPIRE_REMAINING"
    forfeited="$EXPIRE_ENFORCED"
    paused="$(get_session_field "$mac" paused)"
    unlock_mac

    # IMPORTANT:
    # status() must remain read-only and fast.
    # Do NOT call ndsctl auth/deauth here.

    [ "$paused" = "1" ] && is_paused=true || is_paused=false

    if [ "$remaining" -gt 0 ]; then
      lock_state
      state_load_all
      ptotal=0
      [ "$mac" = "$ST_WAITING_MAC" ] && ptotal="$ST_PAYMENT_TOTAL"
      unlock_state
      http_json "200 OK" "{\"status\":\"authorized\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining,\"payment_total\":$ptotal,\"is_paused\":$is_paused}"
    else
      http_json "200 OK" "{\"status\":\"unauthorized\",\"mac\":\"$mac\",\"seconds_remaining\":0,\"payment_total\":0,\"is_paused\":$is_paused}"
    fi
    ;;

  whoami)
    mac=""
    if [ -n "$CLIENT_IP" ]; then
      mac="$(awk -v ip="$CLIENT_IP" '$1==ip{print $4}' /proc/net/arp)"
    fi
    if [ -z "$mac" ] || [ "$mac" = "00:00:00:00:00:00" ]; then
      http_json "200 OK" '{"status":"error","message":"MAC not resolved"}'
      exit 0
    fi
    mac="$(normalize_mac "$mac")"
    http_json "200 OK" "{\"status\":\"ok\",\"mac\":\"$mac\"}"
    ;;

  rates)
    rates_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.rates')"
    http_json "200 OK" "{\"status\":\"ok\",\"rates\":$rates_json}"
    ;;

  update_rates)
    if [ -z "$BODY" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"Rates array required"}'
      exit 0
    fi

    new_rates="["
    first=1
    i=0
    ok=1

    while :; do
      price="$(printf '%s' "$BODY" | jsonfilter -e "@[$i].price" 2>/dev/null)"
      [ -z "$price" ] && break

      minutes="$(printf '%s' "$BODY" | jsonfilter -e "@[$i].minutes" 2>/dev/null)"
      name="$(printf '%s' "$BODY" | jsonfilter -e "@[$i].name" 2>/dev/null)"
      expiration="$(printf '%s' "$BODY" | jsonfilter -e "@[$i].expiration" 2>/dev/null)"

      if [ -z "$minutes" ]; then
        ok=0
        break
      fi

      secs="$(awk "BEGIN{printf \"%d\", $minutes*60}" 2>/dev/null)"

      if [ -z "$secs" ] || [ "$price" -le 0 ] 2>/dev/null || [ "$secs" -le 0 ] 2>/dev/null; then
        ok=0
        break
      fi

      expire_secs=0
      case "$expiration" in
        ''|*[!0-9.]*) expire_secs=0 ;;
        *) expire_secs=$(awk "BEGIN{printf \"%d\", $expiration*60}" 2>/dev/null); [ -z "$expire_secs" ] && expire_secs=0 ;;
      esac

      label="$name"
      [ -z "$label" ] && label="$price peso / $minutes minutes"

      [ "$first" = "1" ] || new_rates="$new_rates,"

      new_rates="$new_rates{\"peso\":$price,\"seconds\":$secs,\"label\":\"$label\",\"expire_seconds\":$expire_secs}"

      first=0
      i=$((i+1))
    done

    new_rates="$new_rates]"

    if [ "$ok" = "0" ] || [ "$first" = "1" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"Invalid rate values"}'
      exit 0
    fi

    lock_config
    admin_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin')"
    dl_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.data_limit')"
    atomic_write "$CONFIG_FILE" <<EOF
{"rates":$new_rates,"admin":$admin_json,"data_limit":$dl_json}
EOF
    unlock_config

    http_json "200 OK" "{\"status\":\"ok\",\"rates\":$new_rates}"
    ;;

  coin_status)
    if [ -f "$COIN_ENABLED_FILE" ]; then
      http_json "200 OK" '{"status":"enabled","coin_enabled":true}'
    else
      http_json "200 OK" '{"status":"disabled","coin_enabled":false}'
    fi
    ;;

  start_payment)
    mac="$(normalize_mac "$Q_MAC")"
    if [ -z "$mac" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"MAC address is required"}'
      exit 0
    fi

    lock_mac "$mac"
    blocked_until="$(get_attempt_field "$mac" blocked_until)"
    unlock_mac
    [ -z "$blocked_until" ] && blocked_until=0
    cur="$(now)"
    if [ "$blocked_until" -gt "$cur" ]; then
      retry=$((blocked_until - cur))
      http_json "429 Too Many Requests" "{\"status\":\"blocked\",\"message\":\"Too many attempts without inserting a coin. Please wait before trying again.\",\"retry_after_seconds\":$retry}"
      exit 0
    fi

    lock_state
    state_load_all
    release_stale_lock

    if [ -n "$ST_WAITING_MAC" ] && [ "$ST_WAITING_MAC" != "$mac" ]; then
      save_state_now
      unlock_state
      http_json "409 Conflict" '{"status":"busy","message":"Another customer is currently paying. Please wait."}'
      exit 0
    fi

    ST_WAITING_MAC="$mac"
    ST_WAITING_SINCE="$cur"
    ST_PAYMENT_TOTAL=0

    touch "$COIN_ENABLED_FILE"

    save_state_now
    unlock_state

    http_json "200 OK" "{\"status\":\"waiting\",\"mac\":\"$mac\",\"payment_total\":0,\"message\":\"Waiting for coin\"}"
    ;;

  coin)
    if [ ! -f "$COIN_ENABLED_FILE" ]; then
      http_json "200 OK" '{"status":"ignored","message":"Coin input is disabled"}'
      exit 0
    fi

    peso="$Q_PESO"
    get_rate_seconds_and_label "$peso"
    if [ -z "$RATE_SECONDS" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"Invalid coin value"}'
      exit 0
    fi

    lock_state
    state_load_all
    release_stale_lock
    ST_ESP_LAST_SEEN="$(now)"

    if [ -z "$ST_WAITING_MAC" ]; then
      save_state_now
      unlock_state
      http_json "400 Bad Request" '{"status":"error","message":"No customer is waiting for payment"}'
      exit 0
    fi

    mac="$ST_WAITING_MAC"
    ST_WAITING_SINCE="$(now)"

    lock_mac "$mac"
    ensure_session "$mac"
    enforce_session_expiration "$mac"
    remaining="$EXPIRE_REMAINING"
    new_remaining=$((remaining + RATE_SECONDS))
    paused="$(get_session_field "$mac" paused)"
    total_paid="$(get_session_field "$mac" total_paid)"; [ -z "$total_paid" ] && total_paid=0
    new_total_paid=$((total_paid + peso))
    cur="$(now)"

    if [ "$remaining" -le 0 ]; then
      session_start_at="$cur"
      session_expire_seconds="$RATE_EXPIRE_SECONDS"
    else
      session_start_at="$(get_session_field "$mac" session_start_at)"; [ -z "$session_start_at" ] && session_start_at=0
      session_expire_seconds="$(get_session_field "$mac" expire_seconds)"; [ -z "$session_expire_seconds" ] && session_expire_seconds=0
    fi

    if [ "$paused" = "1" ]; then
      write_session "$mac" "$(get_session_field "$mac" expires_at)" 1 "$new_remaining" "$new_total_paid" "$cur" "$session_start_at" "$session_expire_seconds"
    else
      new_expires=$((cur + new_remaining))
      write_session "$mac" "$new_expires" 0 0 "$new_total_paid" "$cur" "$session_start_at" "$session_expire_seconds"
      
      ip="$(mac_to_ip "$mac")"
      [ -z "$ip" ] && ip="$CLIENT_IP"

      grant_ip="$ip"
      grant_secs="$new_remaining"
    fi
    unlock_mac

    ST_PAYMENT_TOTAL=$((ST_PAYMENT_TOTAL + peso))
    ST_TOTAL=$((ST_TOTAL + peso))
    ST_DAILY=$((ST_DAILY + peso))
    ST_MONTHLY=$((ST_MONTHLY + peso))
    served_clients_add "$mac"
    save_state_now
    payment_total_out="$ST_PAYMENT_TOTAL"
    unlock_state

    grant_access "$grant_ip" "$grant_secs"

    if [ "$new_remaining" -gt 0 ] 2>/dev/null; then
        schedule_deauth "$mac" "$new_remaining"
    fi

    http_json "200 OK" "{\"status\":\"authorized\",\"mac\":\"$mac\",\"peso\":$peso,\"seconds_added\":$RATE_SECONDS,\"seconds_remaining\":$new_remaining,\"payment_total\":$payment_total_out}"
    ;;

  pause)
    mac="$(normalize_mac "$Q_MAC")"
    lock_mac "$mac"
    f="$(session_file "$mac")"
    if [ ! -f "$f" ]; then
      unlock_mac
      http_json "400 Bad Request" '{"status":"error","message":"No session found"}'
      exit 0
    fi
    enforce_session_expiration "$mac"
    paused="$(get_session_field "$mac" paused)"
    if [ "$EXPIRE_ENFORCED" = "1" ]; then
      deauth_client_access "$mac"
      remaining=0
    elif [ "$paused" != "1" ]; then
      remaining="$EXPIRE_REMAINING"
      total_paid="$(get_session_field "$mac" total_paid)"
      write_session "$mac" "$(now)" 1 "$remaining" "$total_paid" "$(get_session_field "$mac" last_coin_at)" "$(get_session_field "$mac" session_start_at)" "$(get_session_field "$mac" expire_seconds)"
      deauth_client_access "$mac"
    else
      remaining="$(get_session_field "$mac" paused_remaining)"
    fi
    unlock_mac
    http_json "200 OK" "{\"status\":\"ok\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining}"
    ;;

  resume)
    mac="$(normalize_mac "$Q_MAC")"
    lock_mac "$mac"
    f="$(session_file "$mac")"
    if [ ! -f "$f" ]; then
      unlock_mac
      http_json "400 Bad Request" '{"status":"error","message":"No session found"}'
      exit 0
    fi
    enforce_session_expiration "$mac"
    paused="$(get_session_field "$mac" paused)"
    total_paid="$(get_session_field "$mac" total_paid)"

    if [ "$EXPIRE_ENFORCED" = "1" ]; then
        remaining=0
        deauth_client_access "$mac"
    elif [ "$paused" = "1" ]; then
        remaining="$EXPIRE_REMAINING"
        new_expires=$(( $(now) + remaining ))
        write_session "$mac" "$new_expires" 0 0 "$total_paid" "$(get_session_field "$mac" last_coin_at)" "$(get_session_field "$mac" session_start_at)" "$(get_session_field "$mac" expire_seconds)"
        ip="$CLIENT_IP"
        [ -z "$ip" ] && ip="$(mac_to_ip "$mac")"
        grant_access "$ip" "$remaining"
        schedule_deauth "$mac" "$remaining"
    else
        remaining="$(get_remaining_seconds "$mac")"
        if [ "$remaining" -le 0 ] 2>/dev/null; then
            deauth_client_access "$mac"
        fi
    fi

    unlock_mac
    http_json "200 OK" "{\"status\":\"ok\",\"mac\":\"$mac\",\"seconds_remaining\":$remaining}"
    ;;

  end_payment)
    mac="$(normalize_mac "$Q_MAC")"
    rm -f "$COIN_ENABLED_FILE"

    lock_state
    state_load_all
    was_waiting=0
    [ "$ST_WAITING_MAC" = "$mac" ] && was_waiting=1
    payment_total_snapshot="$ST_PAYMENT_TOTAL"
    if [ "$was_waiting" = "1" ]; then
      ST_WAITING_MAC=""
      ST_WAITING_SINCE=0
      save_state_now
    fi
    unlock_state

    if [ "$was_waiting" = "1" ]; then
      lock_mac "$mac"
      if [ "$payment_total_snapshot" = "0" ]; then
        fail_count="$(get_attempt_field "$mac" fail_count)"; [ -z "$fail_count" ] && fail_count=0
        fail_count=$((fail_count + 1))
        blocked_until=0
        if [ "$fail_count" -ge "$MAX_FAILED_ATTEMPTS" ]; then
          blocked_until=$(( $(now) + BLOCK_DURATION_S ))
          fail_count=0
        fi
        write_attempt "$mac" "$fail_count" "$blocked_until"
      else
        clear_attempt "$mac"
      fi
      unlock_mac
    fi

    http_json "200 OK" "{\"status\":\"ok\",\"mac\":\"$mac\"}"
    ;;

  kick)
    mac="$(normalize_mac "$Q_MAC")"
    lock_mac "$mac"
    f="$(session_file "$mac")"
    if [ -f "$f" ]; then
      total_paid="$(get_session_field "$mac" total_paid)"
      write_session "$mac" "$(now)" 0 0 "$total_paid" "$(get_session_field "$mac" last_coin_at)" "$(now)" 0
      deauth_client_access "$mac"
    fi
    unlock_mac
    http_json "200 OK" "{\"status\":\"ok\",\"mac\":\"$mac\"}"
    ;;

  reset_stats)
    lock_state
    state_load_all
    case "$Q_WHICH" in
      total) ST_TOTAL=0 ;;
      daily) ST_DAILY=0 ;;
      monthly) ST_MONTHLY=0 ;;
      *)
        unlock_state
        http_json "400 Bad Request" '{"status":"error","message":"Invalid \"which\" parameter"}'
        exit 0
        ;;
    esac
    save_state_now
    unlock_state
    http_json "200 OK" "{\"status\":\"ok\",\"which\":\"$Q_WHICH\"}"
    ;;

  heartbeat)
    lock_state
    state_load_all
    ST_ESP_LAST_SEEN="$(now)"
    save_state_now
    unlock_state
    http_json "200 OK" '{"status":"ok"}'
    ;;

  get_data_limit)
    dl_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.data_limit')"
    http_json "200 OK" "{\"status\":\"ok\",\"data_limit\":$dl_json}"
    ;;

  set_data_limit)
    upload="$Q_UPLOAD"
    download="$Q_DOWNLOAD"
    if [ -z "$upload" ] || [ -z "$download" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"upload and download are required"}'
      exit 0
    fi
    lock_config
    rates_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.rates')"
    admin_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin')"
    atomic_write "$CONFIG_FILE" <<EOF
{"rates":$rates_json,"admin":$admin_json,"data_limit":{"upload":"$upload","download":"$download"}}
EOF
    unlock_config
    http_json "200 OK" "{\"status\":\"ok\",\"data_limit\":{\"upload\":\"$upload\",\"download\":\"$download\"}}"
    ;;

  login)
    username="$(body_field username)"
    password="$(body_field password)"
    admin_user="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin.username')"
    admin_pass="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin.password')"
    if [ -z "$username" ] || [ -z "$password" ]; then
      http_json "400 Bad Request" '{"status":"error","message":"Username and password are required"}'
      exit 0
    fi
    if [ "$username" = "$admin_user" ] && [ "$password" = "$admin_pass" ]; then
      http_json "200 OK" "{\"status\":\"ok\",\"username\":\"$username\"}"
    else
      http_json "401 Unauthorized" '{"status":"error","message":"Invalid username or password"}'
    fi
    ;;

  change_password)
    username="$(body_field username)"
    current_password="$(body_field current_password)"
    new_password="$(body_field new_password)"

    lock_config
    admin_user="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin.username')"
    admin_pass="$(jsonfilter -i "$CONFIG_FILE" -e '@.admin.password')"

    if [ -z "$username" ] || [ -z "$current_password" ] || [ -z "$new_password" ]; then
      unlock_config
      http_json "400 Bad Request" '{"status":"error","message":"All fields are required"}'
      exit 0
    fi
    if [ "$username" != "$admin_user" ] || [ "$current_password" != "$admin_pass" ]; then
      unlock_config
      http_json "401 Unauthorized" '{"status":"error","message":"Current password is incorrect"}'
      exit 0
    fi
    if [ "${#new_password}" -lt 4 ]; then
      unlock_config
      http_json "400 Bad Request" '{"status":"error","message":"New password must be at least 4 characters"}'
      exit 0
    fi

    rates_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.rates')"
    dl_json="$(jsonfilter -i "$CONFIG_FILE" -e '@.data_limit')"
    atomic_write "$CONFIG_FILE" <<EOF
{"rates":$rates_json,"admin":{"username":"$username","password":"$new_password"},"data_limit":$dl_json}
EOF
    unlock_config
    http_json "200 OK" '{"status":"ok","message":"Password updated"}'
    ;;

  *)
    http_json "400 Bad Request" '{"status":"error","message":"Unknown action"}'
    ;;
esac
