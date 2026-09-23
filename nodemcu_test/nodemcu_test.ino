#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <WiFiManager.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

char serverIP[16] = "192.168.1.1";
char serverPort[6] = "8888";

bool shouldSaveConfig = false;

const int CONFIG_BUTTON_PIN = 0; // FLASH button / GPIO0
const int COIN_PIN = 4;          // D2 = GPIO4

volatile unsigned long pulseCount = 0;
volatile unsigned long lastPulseTime = 0;

const unsigned long DEBOUNCE_MS = 40;
const unsigned long PULSE_TIMEOUT_MS = 350;

bool countingActive = false;

unsigned long lastHeartbeat = 0;
const unsigned long HEARTBEAT_INTERVAL_MS = 15000;

void IRAM_ATTR coinInterrupt() {
  unsigned long now = millis();

  if (now - lastPulseTime >= DEBOUNCE_MS) {
    pulseCount++;
    lastPulseTime = now;
  }
}

void saveConfig() {
  StaticJsonDocument<256> doc;
  doc["server_ip"] = serverIP;
  doc["server_port"] = serverPort;

  File configFile = LittleFS.open("/config.json", "w");
  if (configFile) {
    serializeJson(doc, configFile);
    configFile.close();
    Serial.println("Config saved to flash.");
  } else {
    Serial.println("Failed to open config.json for writing.");
  }
}

void loadConfig() {
  if (!LittleFS.begin()) {
    Serial.println("LittleFS mount failed.");
    return;
  }

  if (LittleFS.exists("/config.json")) {
    File configFile = LittleFS.open("/config.json", "r");
    if (configFile) {
      StaticJsonDocument<256> doc;
      DeserializationError err = deserializeJson(doc, configFile);

      if (!err) {
        strlcpy(serverIP, doc["server_ip"] | serverIP, sizeof(serverIP));
        strlcpy(serverPort, doc["server_port"] | serverPort, sizeof(serverPort));

        Serial.println("Loaded config from flash:");
        Serial.print("  server_ip: ");
        Serial.println(serverIP);
        Serial.print("  server_port: ");
        Serial.println(serverPort);
      } else {
        Serial.println("Failed to parse config.json, using defaults.");
      }

      configFile.close();
    }
  } else {
    Serial.println("No saved config found, using defaults.");
  }

  if (strcmp(serverIP, "192.168.1.1") == 0 && strcmp(serverPort, "80") == 0) {
    strlcpy(serverPort, "8888", sizeof(serverPort));
    saveConfig();
    Serial.println("Migrated backend port from 80 to 8888.");
  }
}

void saveConfigCallback() {
  shouldSaveConfig = true;
}

void setupWiFi() {
  pinMode(CONFIG_BUTTON_PIN, INPUT_PULLUP);

  bool forceConfigPortal = digitalRead(CONFIG_BUTTON_PIN) == LOW;

  WiFiManager wm;

  // Give the main router up to 2 minutes to become available.
  // Only after this timeout should RichFi-Setup appear.
  wm.setConnectTimeout(150);

  // Optional: don't let the configuration portal stay open forever.
  wm.setConfigPortalTimeout(300);

  WiFiManagerParameter customServerIP(
    "server_ip",
    "Backend Server IP",
    serverIP,
    16
  );

  WiFiManagerParameter customServerPort(
    "server_port",
    "Backend Server Port",
    serverPort,
    6
  );

  wm.addParameter(&customServerIP);
  wm.addParameter(&customServerPort);

  wm.setSaveConfigCallback(saveConfigCallback);

  bool connected;

  if (forceConfigPortal) {
    Serial.println();
    Serial.println("================================");
    Serial.println(" CONFIG BUTTON PRESSED");
    Serial.println(" Opening RichFi-Setup...");
    Serial.println("================================");

    connected = wm.startConfigPortal("RichFi-Setup");

  } else {

    Serial.println();
    Serial.println("================================");
    Serial.println(" RichFi WiFi Startup");
    Serial.println(" Waiting up to 120 seconds");
    Serial.println(" for main WiFi router...");
    Serial.println("================================");

    connected = wm.autoConnect("RichFi-Setup");
  }

  if (!connected) {
    Serial.println();
    Serial.println("WiFi connection failed.");
    Serial.println("Restarting...");
    delay(3000);
    ESP.restart();
  }

  // Get the values entered in WiFiManager
  strlcpy(
    serverIP,
    customServerIP.getValue(),
    sizeof(serverIP)
  );

  strlcpy(
    serverPort,
    customServerPort.getValue(),
    sizeof(serverPort)
  );

  if (shouldSaveConfig) {
    saveConfig();
  }

  Serial.println();
  Serial.println("==============================");
  Serial.println("       WiFi Connected!");
  Serial.println("==============================");

  Serial.print("SSID: ");
  Serial.println(WiFi.SSID());

  Serial.print("NodeMCU IP: ");
  Serial.println(WiFi.localIP());

  Serial.print("Gateway: ");
  Serial.println(WiFi.gatewayIP());

  Serial.print("Backend target: http://");
  Serial.print(serverIP);
  Serial.print(":");
  Serial.println(serverPort);

  Serial.println("==============================");
}

int pulsesToPeso(unsigned long pulses) {
  if (pulses == 1) return 1;
  if (pulses == 5) return 5;
  if (pulses == 10) return 10;

  Serial.print("Unrecognized pulse count: ");
  Serial.println(pulses);
  return 0;
}

String buildURL(const String& queryString) {
  return "http://" + String(serverIP) + ":" + String(serverPort) +
         "/cgi-bin/pisowifi?" + queryString;
}

void httpGET(const String& url, const char* label) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.print(label);
    Serial.println(" failed: WiFi disconnected.");
    return;
  }

  WiFiClient client;
  HTTPClient http;

  Serial.println();
  Serial.print(label);
  Serial.println(" request:");
  Serial.println(url);

  http.begin(client, url);
  int httpCode = http.GET();

  Serial.print(label);
  Serial.print(" HTTP code: ");
  Serial.println(httpCode);

  if (httpCode > 0) {
    String response = http.getString();
    Serial.print(label);
    Serial.println(" response:");
    Serial.println(response);
  } else {
    Serial.print(label);
    Serial.print(" error: ");
    Serial.println(http.errorToString(httpCode));
  }

  http.end();
}

void sendCoin(int pesoValue) {
  if (pesoValue <= 0) {
    Serial.println("Skipping send, invalid peso value.");
    return;
  }

  String url = buildURL("action=coin&peso=" + String(pesoValue));
  httpGET(url, "COIN");
}

void sendHeartbeat() {
  String url = buildURL("action=heartbeat");
  httpGET(url, "HEARTBEAT");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println("==============================");
  Serial.println("       RichFi Coin Reader");
  Serial.println("==============================");

  loadConfig();
  setupWiFi();

  pinMode(COIN_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(COIN_PIN), coinInterrupt, FALLING);

  Serial.println("Coin input ready on D2 / GPIO4.");
  Serial.println("Type c in Serial Monitor to send a test 1-peso coin.");
}

void loop() {
  if (Serial.available()) {
    char ch = Serial.read();
    if (ch == 'c' || ch == 'C') {
      Serial.println("Manual serial test: sending 1 peso.");
      sendCoin(1);
    }
  }

  if (pulseCount > 0) {
    countingActive = true;
  }

  if (countingActive) {
    unsigned long timeSinceLastPulse = millis() - lastPulseTime;

    if (timeSinceLastPulse >= PULSE_TIMEOUT_MS) {
      noInterrupts();
      unsigned long finalCount = pulseCount;
      pulseCount = 0;
      interrupts();

      countingActive = false;

      Serial.println();
      Serial.println("==============================");
      Serial.print("COIN BURST DETECTED: ");
      Serial.print(finalCount);
      Serial.println(" pulses");

      int peso = pulsesToPeso(finalCount);

      if (peso > 0) {
        Serial.print("VALUE: P");
        Serial.println(peso);
        Serial.println("==============================");
        sendCoin(peso);
      } else {
        Serial.println("VALUE: unrecognized, ignored");
        Serial.println("==============================");
      }
    }
  }

  if (millis() - lastHeartbeat >= HEARTBEAT_INTERVAL_MS) {
    sendHeartbeat();
    lastHeartbeat = millis();
  }

  delay(10);
}