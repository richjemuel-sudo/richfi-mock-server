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

// Simple sales counters
let totalSales = 0;
let dailySales = 0;
let monthlySales = 0;

// Unique clients that have successfully purchased WiFi service
const servedClients = new Set();

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
| Anti-Abuse: Repeated "Insert Coin" Clicks Without a Coin
|--------------------------------------------------------------------------
|
| Tracks, per MAC, how many payment windows in a row closed (cancelled or
| timed out) with zero coins inserted. After MAX_FAILED_ATTEMPTS in a row,
| that MAC is blocked from starting a new payment window for BLOCK_DURATION_MS.
| Any successful coin insertion resets the count to 0.
|
*/
const paymentAttempts = new Map(); // mac -> { failCount, blockedUntil }
const MAX_FAILED_ATTEMPTS = 6;
const BLOCK_DURATION_MS = 3 * 60 * 1000; // 3 minutes

// ------------------------- DATA LIMIT -------------------------------------
let DATA_LIMIT = { upload: "5Mbps", download: "5Mbps" }; // default

// ------------------------- ADMIN CREDENTIALS -------------------------------
let ADMIN_CREDENTIALS = { username: "admin", password: "admin" }; // default, changeable via admin panel
/*
|--------------------------------------------------------------------------
| Helper Functions
|--------------------------------------------------------------------------
*/

const fs = require("fs");
const STATE_FILE = "./state.json";

  function saveState() {
    const state = {
      sessions: Object.fromEntries(sessions),
      totalSales,
      dailySales,
      monthlySales,
      servedClients: Array.from(servedClients),
      rates: RATES,
      dataLimit: DATA_LIMIT,
      adminCredentials: ADMIN_CREDENTIALS,
    };
    fs.writeFileSync(STATE_FILE, JSON.stringify(state));
  }


  // ------ Wifi rates----------
  function getRate(peso) {
    return RATES.find((rate) => rate.peso === Number(peso));
  }

  function loadState() {
    if (fs.existsSync(STATE_FILE)) {
      try {
        const state = JSON.parse(fs.readFileSync(STATE_FILE, "utf8"));

        if (state.sessions) {
          for (const [mac, session] of Object.entries(state.sessions)) {
            sessions.set(mac, session);
          }
        }
        if (typeof state.totalSales === "number") totalSales = state.totalSales;
        if (typeof state.dailySales === "number") dailySales = state.dailySales;
        if (typeof state.monthlySales === "number") monthlySales = state.monthlySales;
        if (Array.isArray(state.servedClients)) {
          for (const clientMac of state.servedClients) {
            servedClients.add(clientMac);
          }
        }
        if (Array.isArray(state.rates)) RATES = state.rates;
        if (state.adminCredentials && state.adminCredentials.username && state.adminCredentials.password) {
          ADMIN_CREDENTIALS = state.adminCredentials;
        }
        if (state.dataLimit) {
          // Backward-compatible with older state.json files that stored
          // dataLimit as a single "5Mbps / 5Mbps" string.
          if (typeof state.dataLimit === "string") {
            const parts = state.dataLimit.split("/").map(s => s.trim());
            DATA_LIMIT = { upload: parts[0] || "5Mbps", download: parts[1] || "5Mbps" };
          } else {
            DATA_LIMIT = state.dataLimit;
          }
        }

        console.log("[STATE] loaded from disk");
      } catch (err) {
        console.error("[STATE] failed to load:", err);
      }
    }
  }

  loadState(); // call once at startup, before app.listen

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
      try {
        const parsed = req.body; // already parsed by express.json() middleware

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
        saveState(); 
        return res.json({ status: "ok", rates: RATES });

      } catch (err) {
        return res.status(400).json({ status: "error", message: "Invalid JSON body" });
      }
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

      const attempts = paymentAttempts.get(mac);
      if (attempts && attempts.blockedUntil && Date.now() < attempts.blockedUntil) {
        const retryAfterSeconds = Math.ceil((attempts.blockedUntil - Date.now()) / 1000);
        console.log(`[PAYMENT] mac=${mac} blocked - retry in ${retryAfterSeconds}s`);
        return res.status(429).json({
          status: "blocked",
          message: "Too many attempts without inserting a coin. Please wait before trying again.",
          retry_after_seconds: retryAfterSeconds,
        });
      }

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
  const rate = getRate(peso);

  if (!rate) {
    return res.status(400).json({
      status: "error",
      message: "Invalid coin value",
    });
  }

  const secondsAdded = rate.seconds;
  const mac = waitingClientMac;

  // Refresh the payment lock so it doesn't expire while the customer
  // is actively still inserting coins (mirrors the 30s frontend reset).
  waitingSince = Date.now();

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
  monthlySales += peso;
  servedClients.add(mac);

  console.log(
    `[COIN] mac=${mac} peso=${peso} added=${secondsAdded}s remaining=${newRemaining}s paymentTotal=₱${paymentTotal}`
  );

  saveState();

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
  saveState();
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
  saveState();
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

      if (paymentTotal === 0) {
        // Payment window closed with no coin inserted — count it as a failed attempt.
        const attempts = paymentAttempts.get(mac) || { failCount: 0, blockedUntil: 0 };
        attempts.failCount += 1;

        if (attempts.failCount >= MAX_FAILED_ATTEMPTS) {
          attempts.blockedUntil = Date.now() + BLOCK_DURATION_MS;
          attempts.failCount = 0;
          console.log(`[PAYMENT] mac=${mac} blocked for ${BLOCK_DURATION_MS / 1000}s after ${MAX_FAILED_ATTEMPTS} attempts with no coin`);
        }

        paymentAttempts.set(mac, attempts);
      } else {
        // At least one coin was inserted this attempt — they're a genuine customer.
        paymentAttempts.delete(mac);
      }

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
    saveState();
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
      } else if (which === "monthly") {
        monthlySales = 0;
      } else {
        return res.status(400).json({ status: "error", message: "Invalid 'which' parameter" });
      }

      console.log(`[RESET] ${which} sales reset to 0`);
      saveState();
      return res.json({ status: "ok", which });
    }

  //-----------------NODEMCU HEARTBEAT-----------------------------
      case "heartbeat": {
      esp8266LastSeen = Date.now();
      return res.json({ status: "ok" });
    }

  //----------------- DATA LIMIT ------------------------------------
      case "get_data_limit": {
        return res.json({ status: "ok", data_limit: DATA_LIMIT });
      }

      case "set_data_limit": {
        const upload = req.query.upload;
        const download = req.query.download;

        if (!upload || !download) {
          return res.status(400).json({ status: "error", message: "upload and download are required" });
        }

        DATA_LIMIT = { upload, download };
        console.log(`[DATA LIMIT] updated to: ${JSON.stringify(DATA_LIMIT)}`);
        saveState();

        return res.json({ status: "ok", data_limit: DATA_LIMIT });
      }

  //----------------- ADMIN AUTH ------------------------------------
      case "login": {
        const body = req.body || {};
        const username = (body.username || "").trim();
        const password = body.password || "";

        if (!username || !password) {
          return res.status(400).json({ status: "error", message: "Username and password are required" });
        }

        if (username === ADMIN_CREDENTIALS.username && password === ADMIN_CREDENTIALS.password) {
          console.log(`[ADMIN] login ok user=${username}`);
          return res.json({ status: "ok", username });
        }

        console.log(`[ADMIN] login failed user=${username}`);
        return res.status(401).json({ status: "error", message: "Invalid username or password" });
      }

      case "change_password": {
        const body = req.body || {};
        const username = (body.username || "").trim();
        const currentPassword = body.current_password || "";
        const newPassword = body.new_password || "";

        if (!username || !currentPassword || !newPassword) {
          return res.status(400).json({ status: "error", message: "All fields are required" });
        }

        if (username !== ADMIN_CREDENTIALS.username || currentPassword !== ADMIN_CREDENTIALS.password) {
          return res.status(401).json({ status: "error", message: "Current password is incorrect" });
        }

        if (newPassword.length < 4) {
          return res.status(400).json({ status: "error", message: "New password must be at least 4 characters" });
        }

        ADMIN_CREDENTIALS = { username, password: newPassword };
        console.log(`[ADMIN] password changed for user=${username}`);
        saveState();

        return res.json({ status: "ok", message: "Password updated" });
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
    monthly_sales: monthlySales,
    active_users: devices.length,
    total_clients_served: servedClients.size,
    esp8266_status:
      Date.now() - esp8266LastSeen < 30000
        ? "online"
        : "offline",
    devices,
  });
});

// ---------------------------------------------------------
// GET /cgi-bin/reset-clients
// Reset Total Clients Served counter
// ---------------------------------------------------------
app.get("/cgi-bin/reset-clients", (req, res) => {
  servedClients.clear();
  saveState(); // persist so the reset survives a server restart

  console.log("[ADMIN] Total Clients Served reset to 0");

  return res.json({
    status: "ok",
    total_clients_served: 0,
    message: "Total Clients Served has been reset."
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

// originally, the server was listening on