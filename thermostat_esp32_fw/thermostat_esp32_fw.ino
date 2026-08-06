#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Wire.h>
#include <string.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ESP-NOW sender configuration
static const int thisDeviceId = 5;
static const uint8_t espNowBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status);

// DS18B20 sensors
static const uint8_t PIN_DQ_OUT = 16;         // thermometer OUTSIDE the box
static const uint8_t PIN_DQ_IN = 21;          // thermometer INSIDE the box

// relays
static const uint8_t PIN_RELAY_FAN = 17;      // set to H to turn on the fan
static const uint8_t PIN_RELAY_HEATER = 19;   // set to H to turn on the heater

// MAX7219 display connection
static const uint8_t PIN_CLK = 18;
static const uint8_t PIN_CS = 22;
static const uint8_t PIN_DIN = 23;

OneWire oneWireOut(PIN_DQ_OUT);
OneWire oneWireIn(PIN_DQ_IN);

DallasTemperature sensorOut(&oneWireOut);
DallasTemperature sensorIn(&oneWireIn);

enum Error {ERROR_ESP, ERROR_TEMP_IN, ERROR_TEMP_OUT};
enum Action {ACTION_NOOP, ACTION_COOL, ACTION_HEAT};

void setup() {
  uint32_t start = millis();
  Serial.begin(115200);

  Serial.println("Config pins");

  uint8_t pins[5] = {PIN_RELAY_FAN, PIN_RELAY_HEATER, PIN_CLK, PIN_CS, PIN_DIN};

  for(int i=0; i<5; i++) {
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], LOW);
  }

  sensorOut.setResolution(10);    // Set 10-bit resolution (0.25 °C step, 187.5ms conversion)
  sensorIn.setResolution(10);     // Set 10-bit resolution (0.25 °C step, 187.5ms conversion)

  // Use the internal hardware noise for a better seed
  randomSeed(analogRead(0));

  Serial.println("Config wifi");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(10);

  if (esp_now_init() != 0) {
    Serial.println("ESP-NOW init failed");
    showError(ERROR_ESP);
    delay(1000);
    ESP.restart();
  }

  // Register send callback (ESP32-C3 expects `const` + `esp_now_send_status_t`).
  esp_now_register_send_cb(onDataSent);

  // Register broadcast peer.
  // We set peer.channel = 0 so ESPNOW uses the *current* WiFi channel set by esp_wifi_set_channel().
  esp_now_peer_info_t peerInfo{};
  memcpy(peerInfo.peer_addr, espNowBroadcastMac, sizeof(espNowBroadcastMac));
  peerInfo.ifidx = WIFI_IF_STA;
  peerInfo.channel = 0;        // use current WiFi channel
  peerInfo.encrypt = false;
  memset(peerInfo.lmk, 0, sizeof(peerInfo.lmk));

  if (esp_now_add_peer(&peerInfo) != 0) {
    Serial.println("ESP-NOW add broadcast peer failed");
    showError(ERROR_ESP);
    delay(1000);
    ESP.restart();
  }

  Serial.println("Config DONE!");
  uint32_t end = millis();
}

void reportViaEspNow(float tempIn, float tempOut, char action)
{
  const uint32_t packetRandomId = ((uint32_t)random(0xFFFF) << 16) | (uint32_t)random(0xFFFF);

  Serial.println("create payload");

  // Construct JSON into a null-terminated char buffer, then send the bytes
  char payload[250];

  snprintf(payload, sizeof(payload),
          "{\"type\":\"temp_sensor\",\"dev_id\":%d,\"packet_id\":%08lu,\"temp\":%.1f,\"temp_out\":%.1f,\"action\":\"%c\"}\n",
          thisDeviceId,
          (unsigned long) packetRandomId,
          (double) tempIn,
          (double) tempOut,
          action);

  Serial.print("payload: ");
  Serial.println(payload);

  const size_t payloadLen = strlen(payload);

  Serial.println("esp-now send");

  // Send the same ESP-NOW broadcast on the requested WiFi channels.
  const uint8_t channels[3] = {1, 6, 11};
  for (int i = 0; i < 3; i++) {
    const uint8_t ch = channels[i];
    // Switch WiFi channel so ESPNOW transmissions go out on that channel.
    // peerInfo.channel=0 means the peer uses the current channel.
    esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    if (err != 0) {
      Serial.print("esp_wifi_set_channel failed, ch=");
      Serial.print(ch);
      Serial.print(" err=");
      Serial.println((int)err);
    }
    delay(20); // give the radio a moment to switch channels

    // Broadcast MAC
    esp_now_send(espNowBroadcastMac, reinterpret_cast<const uint8_t*>(payload), payloadLen);

    // Small visible pacing
    delay(10);
  }
}

void showError(Error errorNo)
{
  // show error message on display
  // TODO:

  // on error, both turn relays off
  digitalWrite(PIN_RELAY_HEATER, LOW);
  digitalWrite(PIN_RELAY_FAN, LOW);
}

void handleFan(Action action)
{
  static bool fanIsOn = false;          // Track physical pin state
  static uint32_t lastToggleMs = 0;     // Timestamp of LAST STATE CHANGE

  // allow immediate activation at boot
  if (lastToggleMs == 0) {
    lastToggleMs = millis() - 30000; 
  }

  // ignore request if fan is already in the requested state
  if ((fanIsOn && action == ACTION_COOL) || (!fanIsOn && action != ACTION_COOL)) {
    return;
  }

  // enforce 30-second dwell time since last TOGGLE
  uint32_t now = millis();
  if ((now - lastToggleMs) < 30000) {   // state changed less than 30s ago, ignore
    return;
  }

  // apply state change and record timestamp

  if(action == ACTION_COOL) {
    digitalWrite(PIN_RELAY_FAN, HIGH);
    fanIsOn = true;
    Serial.println("Fan switched ON");
  } else {
    digitalWrite(PIN_RELAY_FAN, LOW);
    fanIsOn = false;
    Serial.println("Fan switched OFF");
  }
}

#define REPORT_INTERVAL_MS      (15 * 60 * 1000)

void loop() {
  Serial.println("main start");

  uint32_t lastMs = millis();
  uint32_t lastReportMs = millis() - REPORT_INTERVAL_MS;

  while(1) {
    uint32_t now = millis();
    uint32_t diff = now - lastMs;

    if(diff < 1000) {                 // less than second ago? do nothing
      continue;
    }

    lastMs = millis();

    sensorIn.requestTemperatures();   // Synchronous call (blocks for ~187ms at 10-bit)
    sensorOut.requestTemperatures();  // Synchronous call (blocks for ~187ms at 10-bit)
  
    float tempIn = sensorIn.getTempCByIndex(0);
    float tempOut = sensorOut.getTempCByIndex(0);

    if(tempIn < -30 || tempIn > 60) {     // temp seems wrong? show error, do nothing
      showError(ERROR_TEMP_IN);
      continue;
    }

    if(tempOut < -30 || tempOut > 60) {   // temp seems wrong? show error, do nothing
      showError(ERROR_TEMP_OUT);
      continue;
    }

    // decide on action that needs to be taken
    Action action = ACTION_NOOP;
    char actionChar = '-';

    if(tempIn < 5.0) {              // inside temperature too low? turn on heating
      action = ACTION_HEAT;
      actionChar = 'H';
    } else if(tempIn > 35.0) {      // inside temperature too high?

      if(tempOut < tempIn) {        // outside is cooler than inside? we can cool now
        action = ACTION_COOL;
        actionChar = 'C';
      }
      // if outside is hotter than inside, do nothing, no point of blowing hot air inside the cooler box
    }

    // turn on heater if needed, also immediatelly turn off fan
    if(action == ACTION_HEAT) {
      digitalWrite(PIN_RELAY_HEATER, HIGH);
      digitalWrite(PIN_RELAY_FAN, LOW);
    } else {    // not heating, turn off heater
      digitalWrite(PIN_RELAY_HEATER, LOW);
    }

    // turn on fan if should cool down, with 30 seconds dwell time
    handleFan(action);

    // report state via esp-now every now and then
    diff = now - lastReportMs;
    if(diff >= REPORT_INTERVAL_MS) {
      lastReportMs = now;
      reportViaEspNow(tempIn, tempOut, actionChar);
    }

    // TODO: update display with temps and action
  }
}

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status)
{
  Serial.print("ESP-NOW send status: ");
  Serial.println((int)status);
  (void)mac_addr; // mac_addr may be unused (broadcast peer)
}
