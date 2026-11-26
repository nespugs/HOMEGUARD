// HOMEGUARD ESP-01: serial bridge + offline queue + website commands.
// Prototype: test storage, power, TLS and delivery before unattended use.
// UART RX GPIO3 <- Arduino TX through 5V-to-3.3V level shifter.
// UART TX GPIO1 -> Arduino RX (3.3V-high compatibility must be checked).
// Arduino sends: 29.5,68.0,110,40.0\n
// ESP sends: CMD,MODE,AUTO / CMD,MODE,MAINTENANCE / CMD,MODE,SLEEP
//            CMD,OUTPUT,LED,1 / CMD,OUTPUT,FAN,0 / CMD,OUTPUT,BUZZER,1
#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecureBearSSL.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char* API_BASE = "https://homeguard.win";
const char* SENSOR_TOKEN = "YOUR_SENSOR_TOKEN";

const unsigned long LOG_INTERVAL_MS = 180000UL;
const unsigned long COMMAND_INTERVAL_MS = 5000UL;
const unsigned long WIFI_RETRY_MS = 30000UL;
const unsigned long UPLOAD_INTERVAL_MS = 10000UL;
const size_t QUEUE_MAX_BYTES = 120000; // MUST fit verified LittleFS partition
const char* QUEUE_FILE = "/pending.csv";
const char* TMP_FILE = "/pending.tmp";
const char* IRQ_FILE = "/gpio2_events.csv";

volatile bool irqPending = false;
void IRAM_ATTR onGpio2Falling() { irqPending = true; }

String rxLine, latestReading, currentMode = "AUTO";
bool fsReady = false, haveReading = false;
bool requestedLED = false, requestedFan = false, requestedBuzzer = false;
unsigned long lastLog = 0, lastCommand = 0, lastWifi = 0, lastUpload = 0;
unsigned long lastIrq = 0;

bool validReading(const String& s) {
  float t, h, d; int g; char extra;
  return sscanf(s.c_str(), "%f,%f,%d,%f %c", &t, &h, &g, &d, &extra) == 4 &&
         isfinite(t) && isfinite(h) && isfinite(d) && g >= 0;
}

// For a production device, use certificate validation and a supported TLS trust anchor.
// setInsecure() below is for local development ONLY.
bool beginRequest(HTTPClient& http, BearSSL::WiFiClientSecure& client, const String& url) {
  client.setInsecure();
  http.setTimeout(6000);
  if (!http.begin(client, url)) return false;
  http.addHeader("X-Sensor-Token", SENSOR_TOKEN);
  return true;
}

bool queueReading(const String& line) {
  if (!fsReady || !validReading(line)) return false;
  File f = LittleFS.open(QUEUE_FILE, "r");
  size_t used = f ? f.size() : 0;
  if (f) f.close();
  if (used + line.length() + 2 > QUEUE_MAX_BYTES) return false;
  f = LittleFS.open(QUEUE_FILE, "a");
  if (!f) return false;
  size_t written = f.println(line);
  f.close();
  return written >= line.length() + 1;
}

bool uploadOne(const String& line) {
  if (WiFi.status() != WL_CONNECTED) return false;
  float t,h,d; int g;
  if (sscanf(line.c_str(), "%f,%f,%d,%f", &t,&h,&g,&d) != 4) return false;
  String payload = "{\"temperature\":" + String(t,2) +
    ",\"humidity\":" + String(h,2) + ",\"gas\":" + String(g) +
    ",\"distance\":" + String(d,2) + "}";
  BearSSL::WiFiClientSecure client;
  HTTPClient http;
  if (!beginRequest(http, client, String(API_BASE) + "/api/sensors")) return false;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(payload);
  http.end();
  return code >= 200 && code < 300;
}

bool popConfirmed() {
  File in = LittleFS.open(QUEUE_FILE, "r");
  if (!in) return false;
  File out = LittleFS.open(TMP_FILE, "w");
  if (!out) { in.close(); return false; }
  in.readStringUntil('\n');
  while (in.available()) {
    String line = in.readStringUntil('\n');
    if (line.length()) out.println(line);
    yield();
  }
  in.close(); out.close();
  // Not power-loss atomic; a crash can cause duplicates or lost queue entries.
  LittleFS.remove(QUEUE_FILE);
  return LittleFS.rename(TMP_FILE, QUEUE_FILE);
}

void uploadPending() {
  if (!fsReady || WiFi.status() != WL_CONNECTED) return;
  for (int i=0; i<3; i++) {
    File f = LittleFS.open(QUEUE_FILE, "r");
    if (!f) return;
    String first = f.readStringUntil('\n');
    f.close(); first.trim();
    if (first.length() == 0) return;
    if (!uploadOne(first) || !popConfirmed()) return;
    yield();
  }
}

void emitMode() { Serial.println("CMD,MODE," + currentMode); }
void emitOutputs() {
  Serial.println(String("CMD,OUTPUT,LED,") + (requestedLED ? "1" : "0"));
  Serial.println(String("CMD,OUTPUT,FAN,") + (requestedFan ? "1" : "0"));
  Serial.println(String("CMD,OUTPUT,BUZZER,") + (requestedBuzzer ? "1" : "0"));
}

void pollWebsiteCommands() {
  if (WiFi.status() != WL_CONNECTED) return;
  BearSSL::WiFiClientSecure client;
  HTTPClient http;
  if (!beginRequest(http, client, String(API_BASE) + "/api/device-commands")) return;
  int code = http.GET();
  if (code == 200) {
    // Requires ArduinoJson 6 or 7, install through Arduino IDE Library Manager.
    DynamicJsonDocument doc(512);
    DeserializationError err = deserializeJson(doc, http.getStream());
    if (!err) {
      String mode = doc["mode"] | "AUTO";
      if (mode == "AUTO" || mode == "MAINTENANCE" || mode == "SLEEP") {
        if (mode != currentMode) {
          currentMode = mode;
          emitMode();
          if (mode == "AUTO") lastLog = millis() - LOG_INTERVAL_MS;
        }
        if (mode == "MAINTENANCE") {
          bool led = doc["requested_outputs"]["led"] | false;
          bool fan = doc["requested_outputs"]["fan"] | false;
          bool buzzer = doc["requested_outputs"]["buzzer"] | false;
          if (led != requestedLED || fan != requestedFan || buzzer != requestedBuzzer) {
            requestedLED = led; requestedFan = fan; requestedBuzzer = buzzer;
            emitOutputs();
          }
        }
      }
    }
  }
  http.end();
}

void logGpio2Interrupt() {
  bool occurred;
  noInterrupts(); occurred = irqPending; irqPending = false; interrupts();
  if (!occurred || millis() - lastIrq < 250) return;
  lastIrq = millis();
  // GPIO2 must remain HIGH at boot; do not hold it LOW during reset.
  if (fsReady) {
    File f = LittleFS.open(IRQ_FILE, "a");
    if (f) { f.print("GPIO2_FALLING,"); f.println(millis()); f.close(); }
  }
  Serial.println("EVENT,GPIO2,FALLING");
}

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(50);
  pinMode(2, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(2), onGpio2Falling, FALLING);
  fsReady = LittleFS.begin();
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  emitMode();
  lastLog = millis();
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      rxLine.trim();
      if (validReading(rxLine)) { latestReading = rxLine; haveReading = true; }
      rxLine = "";
    } else if (c != '\r') {
      if (rxLine.length() < 80) rxLine += c;
      else rxLine = "";
    }
  }
  logGpio2Interrupt();
  unsigned long now = millis();
  if (WiFi.status() != WL_CONNECTED && now - lastWifi >= WIFI_RETRY_MS) {
    lastWifi = now;
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  if (now - lastCommand >= COMMAND_INTERVAL_MS) {
    lastCommand = now;
    pollWebsiteCommands();
  }
  if (currentMode == "AUTO" && now - lastLog >= LOG_INTERVAL_MS) {
    lastLog = now;
    if (haveReading) {
      if (!queueReading(latestReading)) Serial.println("ERROR,QUEUE_FULL_OR_UNAVAILABLE");
    } else Serial.println("ERROR,NO_SENSOR_READING");
  }
  // Always retry historical records, even when currently in SLEEP mode.
  if (now - lastUpload >= UPLOAD_INTERVAL_MS) {
    lastUpload = now;
    uploadPending();
  }
  delay(10);
}
