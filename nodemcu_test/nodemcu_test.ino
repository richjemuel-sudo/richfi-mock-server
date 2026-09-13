#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>

/*
|--------------------------------------------------------------------------
| WiFi Configuration
|--------------------------------------------------------------------------
*/

const char* WIFI_SSID = "PLDT_Home_9D724";
const char* WIFI_PASSWORD = "pldthome";

/*
|--------------------------------------------------------------------------
| RichFi Server
|--------------------------------------------------------------------------
*/

const char* SERVER_IP = "192.168.1.219";
const int SERVER_PORT = 3000;

/*
|--------------------------------------------------------------------------
| Coin Input
|--------------------------------------------------------------------------
|
| D2 = GPIO4
| ALLAN 1239A pulse output -> D2
|
*/

const int COIN_PIN = 4;

/*
|--------------------------------------------------------------------------
| Coin Pulse Counting
|--------------------------------------------------------------------------
|
| The ALLAN 1239A sends a BURST of pulses per coin, not one pulse
| per coin. Number of pulses = coin value (DIP-switch configurable
| on the selector, commonly 1 = ₱1, 5 = ₱5, 10 = ₱10).
|
| Strategy:
|   - Count pulses while they keep arriving.
|   - After PULSE_TIMEOUT_MS of silence, treat the burst as "done"
|     and map the pulse count to a peso value.
|
*/

volatile unsigned long pulseCount = 0;
volatile unsigned long lastPulseTime = 0;

const unsigned long DEBOUNCE_MS = 40;       // ignore pulses closer than this (contact bounce)
const unsigned long PULSE_TIMEOUT_MS = 350; // silence after last pulse = coin burst finished

bool countingActive = false;
unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL_MS = 15000; // every 15s

/*
|--------------------------------------------------------------------------
| NODEMCU
|--------------------------------------------------------------------------
*/
void sendHeartbeat() {
  if (WiFi.status() != WL_CONNECTED) return;

  WiFiClient client;
  HTTPClient http;

  String url = "http://" + String(SERVER_IP) + ":" + String(SERVER_PORT) +
               "/cgi-bin/pisowifi?action=heartbeat";

  http.begin(client, url);
  http.GET();
  http.end();
}

/*
|--------------------------------------------------------------------------
| Interrupt
|--------------------------------------------------------------------------
*/

void IRAM_ATTR coinInterrupt() {

  unsigned long now = millis();

  if (now - lastPulseTime >= DEBOUNCE_MS) {
    pulseCount++;
    lastPulseTime = now;
  }
}

/*
|--------------------------------------------------------------------------
| WiFi Connection
|--------------------------------------------------------------------------
*/

void connectWiFi() {

  Serial.println();
  Serial.print("Connecting to WiFi");

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("WiFi connected!");
  Serial.print("NodeMCU IP: ");
  Serial.println(WiFi.localIP());
}

/*
|--------------------------------------------------------------------------
| Map pulse count -> peso value
|--------------------------------------------------------------------------
|
| Adjust this table to match your ALLAN 1239A DIP switch settings.
| Check the selector's manual / test it with a multimeter+serial log
| first to confirm actual pulse counts per coin.
|
*/

int pulsesToPeso(unsigned long pulses) {

  if (pulses == 1)  return 1;
  if (pulses == 5)  return 5;
  if (pulses == 10) return 10;

  Serial.print("Unrecognized pulse count: ");
  Serial.println(pulses);

  return 0; // unknown burst — ignore it, don't credit anything
}

/*
|--------------------------------------------------------------------------
| Send Coin To Backend
|--------------------------------------------------------------------------
*/

void sendCoin(int pesoValue) {

  if (pesoValue <= 0) {
    Serial.println("Skipping send — invalid peso value.");
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi disconnected.");
    return;
  }

  WiFiClient client;
  HTTPClient http;

  String url =
    "http://" +
    String(SERVER_IP) +
    ":" +
    String(SERVER_PORT) +
    "/cgi-bin/pisowifi?action=coin&mac=AA:BB:CC:DD:EE:FF&peso=" +
    String(pesoValue);

  Serial.println();
  Serial.println("Sending coin to backend...");
  Serial.println(url);

  http.begin(client, url);

  int httpCode = http.GET();

  if (httpCode > 0) {
    Serial.print("HTTP Code: ");
    Serial.println(httpCode);

    String response = http.getString();
    Serial.println("Backend response:");
    Serial.println(response);

  } else {
    Serial.print("HTTP request failed: ");
    Serial.println(http.errorToString(httpCode));
  }

  http.end();
}

/*
|--------------------------------------------------------------------------
| Setup
|--------------------------------------------------------------------------
*/

void setup() {

  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("==============================");
  Serial.println("       RichFi Coin Reader");
  Serial.println("==============================");

  pinMode(COIN_PIN, INPUT_PULLUP);

  attachInterrupt(
    digitalPinToInterrupt(COIN_PIN),
    coinInterrupt,
    FALLING
  );

  connectWiFi();
}

/*
|--------------------------------------------------------------------------
| Main Loop
|--------------------------------------------------------------------------
*/

void loop() {

  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  if (pulseCount > 0) {
    countingActive = true;
  }

  if (countingActive) {

    unsigned long timeSinceLastPulse = millis() - lastPulseTime;

    if (timeSinceLastPulse >= PULSE_TIMEOUT_MS) {

      // Burst finished — snapshot and clear atomically
      noInterrupts();
      unsigned long finalCount = pulseCount;
      pulseCount = 0;
      interrupts();

      countingActive = false;

      Serial.println();
      Serial.println("==============================");
      Serial.print("  COIN BURST DETECTED: ");
      Serial.print(finalCount);
      Serial.println(" pulses");

      int peso = pulsesToPeso(finalCount);

      if (peso > 0) {
        Serial.print("  VALUE: P");
        Serial.println(peso);
        Serial.println("==============================");
        sendCoin(peso);
      } else {
        Serial.println("  VALUE: unrecognized, ignored");
        Serial.println("==============================");
      }
    }
  }

  delay(10);
}