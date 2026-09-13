# RichFi Mock Server — Test Before Flashing

This lets you test the full coin -> session -> portal loop using
your laptop as a stand-in for the OpenWRT router, before touching
the Ruijie RG-EW1200G Pro.

## Setup

1. Install Node.js if you don't have it: https://nodejs.org
2. In this folder, run:
   ```
   npm install
   npm start
   ```
   You should see: `RichFi mock server running on http://0.0.0.0:3000`

3. Find your laptop's LAN IP (the one on your home WiFi):
   - Windows: `ipconfig` -> look for "IPv4 Address" under your WiFi adapter
   - Mac/Linux: `ifconfig` or `ip addr` -> look for your WiFi interface's inet address
   - It will look like `192.168.1.XX`

4. Edit `nodemcu_test.ino`:
   - Set `WIFI_SSID` / `WIFI_PASSWORD` to your home WiFi
   - Set `SERVER_IP` to your laptop's LAN IP from step 3
   - Flash it to your NodeMCU via Arduino IDE

5. Open the portal in a browser to watch it update live:
   ```
   http://<your-laptop-ip>:3000/portal.html?mac=AA:BB:CC:DD:EE:FF
   ```
   (matches the TEST_MAC in the NodeMCU sketch)

6. Trigger your coinslot (or short the coin pin manually) — watch:
   - The terminal running `npm start` log `[COIN] mac=... peso=1 ...`
   - The portal page's status update within ~3 seconds (polling)

## Endpoints this mock server provides (same contract the real router will have)

- `GET /cgi-bin/pisowifi?action=coin&mac=X&peso=1` — simulates a coin insert
- `GET /cgi-bin/pisowifi?action=authorize&mac=X&seconds=N` — grant N seconds directly
- `GET /cgi-bin/pisowifi?action=status&mac=X` — check remaining session time
- `GET /cgi-bin/dashboard` — sales totals + active devices (for admin panel later)

## Moving to the real router later

Once OpenWRT + your CGI script are live on the Ruijie:
1. Change `SERVER_IP` in the NodeMCU sketch to the router's LAN IP (usually `192.168.1.1`)
2. Make sure your real CGI script returns the exact same JSON shape shown above
3. No changes needed to portal.html or admin.html — they already call `/cgi-bin/pisowifi`
