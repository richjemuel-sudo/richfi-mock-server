const express = require("express");
const path = require("path");

const app = express();
app.use(express.static(path.join(__dirname, "public")));
const PORT = 3000;

/*
|--------------------------------------------------------------------------
| RichFi Configuration
|--------------------------------------------------------------------------
*/

const CONFIG = {
  rates: [
    {
      peso: 1,
      seconds: 12 * 60,
      label: "1 peso / 12 minutes",
    },
    {
      peso: 5,
      seconds: 2 * 60 * 60,
      label: "5 pesos / 2 hours",
    },
    {
      peso: 10,
      seconds: 5 * 60 * 60,
      label: "10 pesos / 5 hours",
    },
  ],
};

/*
|--------------------------------------------------------------------------
| In-Memory State
|--------------------------------------------------------------------------
|
| This is only for local development.
|
| Later this will be replaced by persistent storage on OpenWrt.
|
*/

const sessions = new Map();

let totalSales = 0;
let dailySales = 0;

let esp8266LastSeen = Date.now();

/*
|--------------------------------------------------------------------------
| Payment Waiting State
|--------------------------------------------------------------------------
|
| The customer who most recently clicked "INSERT COIN".
|
| Example:
| waitingClientMac = "AA:BB:CC:DD:EE:FF"
|
*/

let waitingClientMac = null;
let waitingSince = null;

/*
|--------------------------------------------------------------------------
| Helper Functions
|--------------------------------------------------------------------------
*/

function normalizeMac(mac) {
  if (!mac) return "";

  return mac
    .trim()
    .toUpperCase();
}

function getRate(peso) {
  return CONFIG.rates.find(
    (rate) => rate.peso === Number(peso)
  );
}

function getRemainingSeconds(session) {
  if (!session) {
    return 0;
  }

  const now = Date.now();

  const remaining = Math.max(
    0,
    Math.floor((session.expiresAt - now) / 1000)
  );

  return remaining;
}

function getOrCreateSession(mac) {
  let session = sessions.get(mac);

  if (!session) {
    session = {
      mac,
      expiresAt: Date.now(),
      totalPaid: 0,
      lastCoinAt: null,
    };

    sessions.set(mac, session);
  }

  return session;
}

/*
|--------------------------------------------------------------------------
| Health Check
|--------------------------------------------------------------------------
*/

app.get("/cgi-bin/health", (req, res) => {
  const espOnline =
    Date.now() - esp8266LastSeen < 30000;

  res.json({
    status: "ok",
    server: "online",
    esp8266_status: espOnline
      ? "online"
      : "offline",
    timestamp: new Date().toISOString(),
  });
});

/*
|--------------------------------------------------------------------------
| RichFi Main API
|--------------------------------------------------------------------------
*/

app.get("/cgi-bin/pisowifi", (req, res) => {
  const action = req.query.action;

  switch (action) {
    /*
    |--------------------------------------------------------------------------
    | STATUS
    |--------------------------------------------------------------------------
    */

    case "status": {
      const mac = normalizeMac(req.query.mac);

      if (!mac) {
        return res.status(400).json({
          status: "error",
          message: "MAC address is required",
        });
      }

      const session = sessions.get(mac);

      const secondsRemaining =
        getRemainingSeconds(session);

      if (secondsRemaining > 0) {
        return res.json({
          status: "authorized",
          mac,
          seconds_remaining: secondsRemaining,
        });
      }

      return res.json({
        status: "unauthorized",
        mac,
        seconds_remaining: 0,
      });
    }

    /*
    |--------------------------------------------------------------------------
    | RATES
    |--------------------------------------------------------------------------
    */

    case "rates": {
      return res.json({
        status: "ok",
        rates: CONFIG.rates,
      });
    }

    /*
|--------------------------------------------------------------------------
| START PAYMENT
|--------------------------------------------------------------------------
*/

case "start_payment": {

  const mac = normalizeMac(req.query.mac);

  if (!mac) {
    return res.status(400).json({
      status: "error",
      message: "MAC address is required",
    });
  }

  /*
   * Register this customer as waiting for a coin.
   */

      waitingClientMac = mac;
      waitingSince = Date.now();

      console.log(
        `[PAYMENT] waiting client=${waitingClientMac}`
      );

      return res.json({
        status: "waiting",
        mac,
        message: "Waiting for coin",
      });
    }
    /*
    |--------------------------------------------------------------------------
    | COIN
    |--------------------------------------------------------------------------
    */

    case "coin": {

      const peso = Number(req.query.peso);

      if (!peso) {
        return res.status(400).json({
          status: "error",
          message: "Coin value is required",
        });
      }

      /*
      * We need a customer waiting for payment.
      */

      if (!waitingClientMac) {

        console.log(
          `[COIN] peso=${peso} rejected - no waiting client`
        );

        return res.status(409).json({
          status: "error",
          message: "No customer is waiting for payment",
        });
      }

      /*
      * Make sure the coin value is valid.
      */

      const rate = getRate(peso);

      if (!rate) {

        return res.status(400).json({
          status: "error",
          message: `Unsupported coin value: ${peso}`,
        });
      }

      /*
      * Record ESP8266 activity.
      */

      esp8266LastSeen = Date.now();

      /*
      * The waiting customer receives the time.
      */

      const mac = waitingClientMac;

      const session = getOrCreateSession(mac);

      /*
      * Preserve existing remaining time.
      */

      const currentRemaining =
        getRemainingSeconds(session);

      /*
      * Add purchased time.
      */

      const newRemaining =
        currentRemaining + rate.seconds;

      session.expiresAt =
        Date.now() + newRemaining * 1000;

      session.totalPaid += peso;
      session.lastCoinAt = Date.now();

      sessions.set(mac, session);

      /*
      * Sales statistics.
      */

      totalSales += peso;
      dailySales += peso;

      console.log(
        `[COIN] mac=${mac} peso=${peso} ` +
        `added=${rate.seconds}s ` +
        `remaining=${newRemaining}s`
      );

      /*
      * Payment completed.
      *
      * Clear waiting client so another customer
      * can start a new payment.
      */

      waitingClientMac = null;
      waitingSince = null;

      return res.status(200).json({
        status: "authorized",
        mac,
        peso,
        seconds_added: rate.seconds,
        seconds_remaining: newRemaining,
      });
    }

    /*
    |--------------------------------------------------------------------------
    | UNKNOWN ACTION
    |--------------------------------------------------------------------------
    */

    default: {
      return res.status(400).json({
        status: "error",
        message: "Unknown action",
      });
    }
  }
});

/*
|--------------------------------------------------------------------------
| ADMIN DASHBOARD
|--------------------------------------------------------------------------
*/

app.get("/cgi-bin/dashboard", (req, res) => {
  const devices = [];

  for (const [mac, session] of sessions.entries()) {
    const remaining =
      getRemainingSeconds(session);

    if (remaining > 0) {
      devices.push({
        mac,
        seconds_remaining: remaining,
        total_paid: session.totalPaid,
      });
    }
  }

  res.json({
    status: "ok",
    total_sales: totalSales,
    daily_sales: dailySales,
    active_users: devices.length,
    esp8266_status:
      Date.now() - esp8266LastSeen < 30000
        ? "online"
        : "offline",
    devices,
  });
});

/*
|--------------------------------------------------------------------------
| Static Frontend
|--------------------------------------------------------------------------
*/

app.use(express.static(path.join(__dirname)));

/*
|--------------------------------------------------------------------------
| Start Server
|--------------------------------------------------------------------------
*/

app.listen(PORT, "0.0.0.0", () => {
  console.log(
    `RichFi mock server running on http://0.0.0.0:${PORT}`
  );

  console.log(
    `Local portal: http://localhost:${PORT}/portal.html`
  );

  console.log(
    `LAN portal: http://192.168.1.208:${PORT}/portal.html`
  );
});