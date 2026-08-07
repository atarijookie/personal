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

#define LEFT  0
#define RIGHT 1

// Define actual ESP32 pins for MAX7219 first
#define MAX_CLK 18
#define MAX_CS  22
#define MAX_DIN 23

// then include the header
#include <max7219.h>
MAX7219 max7219;

OneWire oneWireOut(PIN_DQ_OUT);
OneWire oneWireIn(PIN_DQ_IN);

DallasTemperature sensorOut(&oneWireOut);
DallasTemperature sensorIn(&oneWireIn);

enum Error {ERROR_ESP, ERROR_TEMP_IN, ERROR_TEMP_OUT};
enum Action {ACTION_NOOP, ACTION_COOL, ACTION_HEAT};

bool fanIsOn = false;
bool heaterIsOn = false;

#define TEMP_HEAT_START          5.0
#define TEMP_HEAT_STOP          (TEMP_HEAT_START + 2.0)

#define TEMP_COOL_START         35.0
#define TEMP_COOL_STOP          (TEMP_COOL_START - 2.0)

#define HEAT_COOL_INTERVAL_MS   (30 * 1000)         // 30 seconds
#define REPORT_INTERVAL_MS      (15 * 60 * 1000)    // 15 minutes

void fanSwitch(bool on)
{
  digitalWrite(PIN_RELAY_FAN, on ? HIGH : LOW);
  fanIsOn = on;

  Serial.print("Fan switched ");
  Serial.println(on ? "ON" : "OFF");
}

void heaterSwitch(bool on)
{
  digitalWrite(PIN_RELAY_HEATER, on ? HIGH : LOW);
  heaterIsOn = on;
}

void setup() {
  uint32_t start = millis();
  Serial.begin(115200);

  Serial.println("Config pins");

  uint8_t pins[2] = {PIN_RELAY_FAN, PIN_RELAY_HEATER};

  for(int i=0; i<2; i++) {
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], LOW);
  }

  fanSwitch(false);
  heaterSwitch(false);

  sensorOut.begin();
  sensorIn.begin();

  sensorOut.setResolution(10);    // Set 10-bit resolution (0.25 °C step, 187.5ms conversion)
  sensorIn.setResolution(10);     // Set 10-bit resolution (0.25 °C step, 187.5ms conversion)

  Serial.println("Config display");
  max7219.Begin();  // initialize display
  max7219.Clear();
  max7219.DisplayText("BOOt", LEFT);

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

// report state via esp-now every now and then
void reportViaEspNow(float tempIn, float tempOut, char action)
{
  static uint32_t lastMs = 0;
  uint32_t now = millis();

  if(lastMs == 0) {               // not initialized? initialize to past value to send right after turning on
    lastMs = now - REPORT_INTERVAL_MS;
  }

  uint32_t diff = now - lastMs;
  if(diff < REPORT_INTERVAL_MS) {       // too short after last report? quit
    return;
  }
  lastMs = now;

  uint32_t packetRandomId = esp_random();

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
    delay(100); // give the radio a moment to switch channels

    // Broadcast MAC
    esp_now_send(espNowBroadcastMac, reinterpret_cast<const uint8_t*>(payload), payloadLen);

    // Small visible pacing
    delay(100);
  }
}

void showError(Error errorNo)
{
  // show error message on display
  max7219.Clear();

  switch(errorNo) {
    case ERROR_ESP:       max7219.DisplayText("Err ESP", LEFT); break;
    case ERROR_TEMP_IN:   max7219.DisplayText("Err Tin", LEFT); break;
    case ERROR_TEMP_OUT:  max7219.DisplayText("Err Tout", LEFT); break;
    default:              max7219.DisplayText("Err", LEFT); break;
  }

  // on error, both turn relays off
  fanSwitch(false);
  heaterSwitch(false);
}

void showTempsAndAction(float tempIn, float tempOut, char action)
{
  char buf[9]; // 8 display characters + 1 null terminator (\0)

  Serial.print("temp in:");
  snprintf(buf, sizeof(buf), "%.1f", tempIn);
  Serial.println(buf);

  Serial.print("temp out:");
  snprintf(buf, sizeof(buf), "%.1f", tempOut);
  Serial.println(buf);

  Serial.print("action:");
  Serial.println(action);

  max7219.Clear();

  snprintf(buf, sizeof(buf), "%3d %3d%c", (int)round(tempIn), (int)round(tempOut), action);
  max7219.DisplayText(buf, LEFT);

  Serial.print("display:");
  Serial.println(buf);
}

char handleHeatingCooling(float tempIn, float tempOut)
{
  static char lastActionChar = ' ';
  static uint32_t lastMs = 0;
  uint32_t now = millis();

  if(lastMs == 0) {                       // not initialized? initialize
    lastMs = now - HEAT_COOL_INTERVAL_MS;
  }

  uint32_t diff = now - lastMs;
  if(diff < HEAT_COOL_INTERVAL_MS) {      // too short after last change? quit
    return lastActionChar;
  }
  lastMs = now;

  // decide on action that needs to be taken
  Action action = ACTION_NOOP;
  char actionChar = ' ';

  bool fanCanCool = (tempOut < tempIn);   // fan can cool only if outside temperature is lower than inside temperature, otherwise it's not cooling

  // nothing is on, completely idle
  if(!fanIsOn && !heaterIsOn) {
    if(tempIn < TEMP_HEAT_START) {                // inside temperature too low? turn on heating
      action = ACTION_HEAT;
    }

    if(tempIn > TEMP_COOL_START && fanCanCool) {  // inside temperature too high and outside is cooler than inside? we can cool now
      action = ACTION_COOL;
    }
  }

  // we are currently heating and we have reached stop temperature, we can stop heating
  if(heaterIsOn) {
    action = (tempIn < TEMP_HEAT_STOP) ? ACTION_HEAT : ACTION_NOOP;   // still below heating stop temp? keep heating, otherwise stop
  }

  // we are currently cooling and we have reached stop temperature or we cannot cool anymore, stop cooling
  if(fanIsOn) {
    action = (tempIn > TEMP_COOL_STOP && fanCanCool) ? ACTION_COOL : ACTION_NOOP;
  }

  // based on action, turn on just heater or just fan, or turn both off
  switch(action) {
    case ACTION_HEAT:
      actionChar = 'H';
      heaterSwitch(true);
      fanSwitch(false);
      break;

    case ACTION_COOL:
      actionChar = 'C';
      heaterSwitch(false);
      fanSwitch(true);
      break;

    default:
      actionChar = ' ';
      heaterSwitch(false);
      fanSwitch(false);
      break;
  }

  lastActionChar = actionChar;
  return actionChar;
}

uint32_t lastLoopMs = 0;

void loop()
{
  uint32_t now = millis();
  uint32_t diff = now - lastLoopMs;

  if(diff < 1000) {                 // less than second ago? do nothing
    delay(100);
    return;
  }
  lastLoopMs = now;

  sensorIn.requestTemperatures();   // Synchronous call (blocks for ~187ms at 10-bit)
  sensorOut.requestTemperatures();  // Synchronous call (blocks for ~187ms at 10-bit)

  float tempIn = sensorIn.getTempCByIndex(0);
  float tempOut = sensorOut.getTempCByIndex(0);

  if(tempIn < -30 || tempIn > 60) {     // temp seems wrong? show error, do nothing
    showError(ERROR_TEMP_IN);
    return;
  }

  if(tempOut < -30 || tempOut > 60) {   // temp seems wrong? show error, do nothing
    showError(ERROR_TEMP_OUT);
    return;
  }

  char actionChar = handleHeatingCooling(tempIn, tempOut);

  // report state via esp-now every now and then
  reportViaEspNow(tempIn, tempOut, actionChar);

  // update display with temps and action
  showTempsAndAction(tempIn, tempOut, actionChar);
}

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status)
{
  Serial.print("ESP-NOW send status: ");
  Serial.println((int)status);
  (void)mac_addr; // mac_addr may be unused (broadcast peer)
}
