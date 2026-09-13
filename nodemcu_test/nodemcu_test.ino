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

const char* SERVER_IP = "192.168.1.208";
const int SERVER_PORT = 3000;

/*
|--------------------------------------------------------------------------
| Coin Input
|--------------------------------------------------------------------------
|
| D2 = GPIO4
|
| For now:
| D2 -> GND = simulated coin
|
*/

const int COIN_PIN = 4;

/*
|--------------------------------------------------------------------------
| Coin Detection
|--------------------------------------------------------------------------
*/

volatile bool coinDetected = false;

unsigned long lastCoinTime = 0;

const unsigned long DEBOUNCE_MS = 100;

/*
|--------------------------------------------------------------------------
| Interrupt
|--------------------------------------------------------------------------
*/

void IRAM_ATTR coinInterrupt() {

  unsigned long now = millis();

  if (now - lastCoinTime >= DEBOUNCE_MS) {

    coinDetected = true;

    lastCoinTime = now;
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
| Send Coin To Backend
|--------------------------------------------------------------------------
*/

void sendCoin(int pesoValue) {

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

    Serial.println(
      http.errorToString(httpCode)
    );
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

  /*
   * D2 uses internal pull-up.
   *
   * Normal state:
   * HIGH
   *
   * Connected to GND:
   * LOW
   */

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

  /*
   * Check whether the interrupt detected a coin.
   */

  if (coinDetected) {

    /*
     * Safely clear the flag.
     */

    noInterrupts();

    coinDetected = false;

    interrupts();

    /*
     * Temporary testing:
     *
     * Every detected pulse = ₱1
     */

    Serial.println();
    Serial.println("==============================");
    Serial.println("       COIN DETECTED!");
    Serial.println("       TEST VALUE: P1");
    Serial.println("==============================");

    sendCoin(1);
  }

  delay(20);
}