const express = require("express");
const path = require("path");

const app = express();
app.use(express.static(path.join(__dirname, "public")));
const PORT = 3000;
app.use(express.json());


/*
|--------------------------------------------------------------------------
| RichFi Configuration
|--------------------------------------------------------------------------
*/

let RATES = [
  { peso: 1, seconds: 12 * 60, label: "1 peso / 12 minutes" },
  { peso: 5, seconds: 2 * 60 * 60, label: "5 pesos / 2 hours" },
  { peso: 10, seconds: 5 * 60 * 60, label: "10 pesos / 5 hours" },
];

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
let paymentTotal = 0;

const PAYMENT_LOCK_TIMEOUT_MS = 45000; // slightly longer than the 30s frontend countdown

/*
|--------------------------------------------------------------------------
| Helper Functions
|--------------------------------------------------------------------------
*/

const fs = require("fs");
const SESSIONS_FILE = "./sessions.json";

function saveSessions() {
  const obj = Object.fromEntries(sessions);
  fs.writeFileSync(SESSIONS_FILE, JSON.stringify(obj));
}


// ------ Wifi rates----------
function getRate(peso) {
  return RATES.find((rate) => rate.peso === Number(peso));
}

function loadSessions() {
  if (fs.existsSync(SESSIONS_FILE)) {
    const obj = JSON.parse(fs.readFileSync(SESSIONS_FILE, "utf8"));
    for (const [mac, session] of Object.entries(obj)) {
      sessions.set(mac, session);
    }
  }
}

loadSessions(); // call once at startup, before app.listen

function releaseStaleLock() {
  if (waitingClientMac && Date.now() - waitingSince > PAYMENT_LOCK_TIMEOUT_MS) {
    console.log(`[PAYMENT] stale lock released for mac=${waitingClientMac}`);
    waitingClientMac = null;
    waitingSince = null;
  }
}

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
  if (!session) return 0;
  if (session.isPaused) return session.pausedRemaining || 0;
  if (!session.expiresAt) return 0;

  const now = Date.now();
  return Math.max(0, Math.floor((session.expiresAt - now) / 1000));
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


function handlePisowifi(req, res) {
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
        return res.status(400).json({ status: "error", message: "MAC address is required" });
      }

      const session = sessions.get(mac);
      const secondsRemaining = getRemainingSeconds(session);

      if (secondsRemaining > 0) {
        return res.json({
          status: "authorized",
          mac,
          seconds_remaining: secondsRemaining,
          payment_total: mac === waitingClientMac ? paymentTotal : 0,
          is_paused: session ? !!session.isPaused : false,
        });
      }

      return res.json({
        status: "unauthorized",
        mac,
        seconds_remaining: 0,
        payment_total: 0,
        is_paused: session ? !!session.isPaused : false,
      });
    }

    /*
    |--------------------------------------------------------------------------
    | RATES
    |--------------------------------------------------------------------------
    */

    case "rates": {
      return res.json({ status: "ok", rates: RATES });
    }

    //------------------------- SAVE RATES -------------------------------------
    case "update_rates": {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => {
      try {
        const parsed = JSON.parse(body);

        if (!Array.isArray(parsed) || parsed.length === 0) {
          return res.status(400).json({ status: "error", message: "Rates array required" });
        }

        const newRates = parsed.map(r => ({
          peso: Number(r.price),
          seconds: Math.round(Number(r.minutes) * 60),
          label: r.name || `${r.price} peso / ${r.minutes} minutes`,
        }));

        // Basic validation — reject if any value is invalid
        for (const r of newRates) {
          if (!r.peso || r.peso <= 0 || !r.seconds || r.seconds <= 0) {
            return res.status(400).json({ status: "error", message: "Invalid rate values" });
          }
        }

        RATES = newRates;
        console.log(`[RATES] updated: ${JSON.stringify(RATES)}`);

        return res.json({ status: "ok", rates: RATES });

      } catch (err) {
        return res.status(400).json({ status: "error", message: "Invalid JSON body" });
      }
    });
    return; // important: don't fall through to default, since response is async
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

      releaseStaleLock();   // ← add this line

      if (waitingClientMac && waitingClientMac !== mac) {
        return res.status(409).json({
          status: "busy",
          message: "Another customer is currently paying. Please wait.",
        });
      }

      waitingClientMac = mac;
      waitingSince = Date.now();
      paymentTotal = 0;

      console.log(`[PAYMENT] waiting client=${waitingClientMac}`);

      return res.json({
        status: "waiting",
        mac,
        payment_total: paymentTotal,
        message: "Waiting for coin",
      });
    }
    /*
    |--------------------------------------------------------------------------
    | COIN
    |--------------------------------------------------------------------------
    */

case "coin": {

  releaseStaleLock();
  esp8266LastSeen = Date.now();

  if (!waitingClientMac) {
    console.log(`[COIN] peso=${req.query.peso} rejected - no waiting client`);
    return res.status(400).json({
      status: "error",
      message: "No customer is waiting for payment",
    });
  }

  const peso = parseInt(req.query.peso, 10);

  if (![1, 5, 10].includes(peso)) {
    return res.status(400).json({
      status: "error",
      message: "Invalid coin value",
    });
  }

  const RATE_TABLE = { 1: 720, 5: 7200, 10: 18000 };
  const secondsAdded = RATE_TABLE[peso];
  const mac = waitingClientMac;

  const session = getOrCreateSession(mac);
  const currentRemaining = getRemainingSeconds(session);
  const newRemaining = currentRemaining + secondsAdded;

  if (session.isPaused) {
    session.pausedRemaining = newRemaining;
  } else {
    session.expiresAt = Date.now() + newRemaining * 1000;
  }

  session.totalPaid = (session.totalPaid || 0) + peso;
  session.lastCoinAt = Date.now();

  sessions.set(mac, session);

  paymentTotal += peso;
  totalSales += peso;
  dailySales += peso;

  console.log(
    `[COIN] mac=${mac} peso=${peso} added=${secondsAdded}s remaining=${newRemaining}s paymentTotal=₱${paymentTotal}`
  );

  saveSessions();

  return res.json({
    status: "authorized",
    mac,
    peso,
    seconds_added: secondsAdded,
    seconds_remaining: newRemaining,
    payment_total: paymentTotal,
  });
}


// ------- Paused ------------------------------------------

case "pause": {
  const mac = normalizeMac(req.query.mac);
  const session = sessions.get(mac);

  if (!session) {
    return res.status(400).json({ status: "error", message: "No session found" });
  }

  if (!session.isPaused) {
    session.pausedRemaining = getRemainingSeconds(session);
    session.isPaused = true;
    session.expiresAt = null; // stop the clock
  }

  console.log(`[PAUSE] mac=${mac} remaining=${session.pausedRemaining}s`);
  saveSessions();
  return res.json({
    status: "ok",
    mac,
    seconds_remaining: session.pausedRemaining,
  });
}

case "resume": {
  const mac = normalizeMac(req.query.mac);
  const session = sessions.get(mac);

  if (!session) {
    return res.status(400).json({ status: "error", message: "No session found" });
  }

  if (session.isPaused) {
    session.expiresAt = Date.now() + (session.pausedRemaining || 0) * 1000;
    session.isPaused = false;
  }

  console.log(`[RESUME] mac=${mac} remaining=${getRemainingSeconds(session)}s`);
  saveSessions();
  return res.json({
    status: "ok",
    mac,
    seconds_remaining: getRemainingSeconds(session),
  });
}

// ----------------Insert Coin Timeout-----------------------------------

  case "end_payment": {
    const mac = normalizeMac(req.query.mac);

    if (waitingClientMac === mac) {
      console.log(`[PAYMENT] ended for client=${waitingClientMac}`);
      waitingClientMac = null;
      waitingSince = null;
    }

    return res.json({ status: "ok", mac });
  }

  //// -------------------KICK CLIENT-----------------------------------

  case "kick": {
  const mac = normalizeMac(req.query.mac);
  const session = sessions.get(mac);

  if (session) {
    session.expiresAt = Date.now(); // zero out remaining time immediately
    session.isPaused = false;
    sessions.set(mac, session);
    saveSessions();
  }

  console.log(`[KICK] mac=${mac} force-disconnected`);
  return res.json({ status: "ok", mac });
}

// --------------------- Reset Stats----------------------
    case "reset_stats": {
      const which = req.query.which;

      if (which === "total") {
        totalSales = 0;
      } else if (which === "daily") {
        dailySales = 0;
      } else {
        return res.status(400).json({ status: "error", message: "Invalid 'which' parameter" });
      }

      console.log(`[RESET] ${which} sales reset to 0`);
      return res.json({ status: "ok", which });
    }

  //-----------------NODEMCU HEARTBEAT-----------------------------
      case "heartbeat": {
      esp8266LastSeen = Date.now();
      return res.json({ status: "ok" });
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
};

app.get("/cgi-bin/pisowifi", handlePisowifi);
app.post("/cgi-bin/pisowifi", handlePisowifi);
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