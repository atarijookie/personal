#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Wire.h>
#include <string.h>
#include <OneWire.h>
#include <DallasTemperature.h>

#define REPORT_INTERVAL_MS  (15 * 60 * 1000)

// ESP-NOW sender configuration
static const int thisDeviceId = 7;    // will use this id for water temperature
static const uint8_t espNowBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status);

// DS18B20 sensors
static const uint8_t PIN_DQ = 21;             // thermometer in water

// ADS1115 connection
static const uint8_t PIN_I2C_SDA = 17;
static const uint8_t PIN_I2C_SCL = 19;

#define LEFT  0
#define RIGHT 1

/*
  IMPORTANT! If your esp32 fails to boot, you should edit the max7219.h file
  and replace the existing definitions of MAX_CLK, MAX_CS, MAX_DIN with these below,
  because using GPIO12 makes the code freeze.
*/

// Define actual ESP32 pins for MAX7219
#define MAX_CLK 18
#define MAX_CS  22
#define MAX_DIN 23

#include <max7219.h>
MAX7219 max7219;

OneWire oneWire(PIN_DQ);
DallasTemperature sensorIn(&oneWire);

#include <Adafruit_ADS1X15.h>
Adafruit_ADS1115 ads;  /* Use this for the 16-bit version */

enum Error {ERROR_ESP, ERROR_ADS, ERROR_TEMP_IN, ERROR_TEMP_OUT};

void setup() {
  uint32_t start = millis();
  Serial.begin(115200);

  //---------
  Serial.println("Config DS18B20");

  sensorIn.begin();
  sensorIn.setResolution(10);     // Set 10-bit resolution (0.25 °C step, 187.5ms conversion)

  //---------
  Serial.println("Config display");

  max7219.Begin();  // initialize display
  max7219.Clear();
  max7219.DisplayText("BOOt", LEFT);

  //---------
  Serial.println("Config ADS");

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);   // Initialize I2C bus with custom pins

  // Pass the Wire instance and I2C address (default 0x48) to ads.begin
  if (!ads.begin(0x48, &Wire)) {
    Serial.println("Failed to initialize ADS.");
    showError(ERROR_ADS);
    delay(1000);
    ESP.restart();
  }

  // Set PGA to 2x gain (+/- 2.048V)
  ads.setGain(GAIN_TWO);        // 2x gain   +/- 2.048V  1 bit = 0.0625mV

  //---------
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

void EspNowSend(char* payload, size_t payloadLen)
{
  Serial.print("esp-now send: '");
  Serial.print(payload);
  Serial.println("'");

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

void espNow_temperature(int deviceId, float temperature)
{
  uint32_t packetRandomId = esp_random();

  // Construct JSON into a null-terminated char buffer, then send the bytes
  char payload[250];

  snprintf(payload, sizeof(payload),
          "{\"type\":\"temp_sensor\",\"dev_id\":%d,\"packet_id\":%08lu,\"temp\":%.1f}\n\r",
          deviceId, (unsigned long) packetRandomId, (double) temperature);

  const size_t payloadLen = strlen(payload);
  EspNowSend(payload, payloadLen);
}

void espNow_orp(int deviceId, int orpMiliVolts)
{
  uint32_t packetRandomId = esp_random();

  // Construct JSON into a null-terminated char buffer, then send the bytes
  char payload[250];

  snprintf(payload, sizeof(payload),
          "{\"type\":\"orp_sensor\",\"dev_id\":%d,\"packet_id\":%08lu,\"orp\":%d}\n\r",
          deviceId, (unsigned long) packetRandomId, orpMiliVolts);

  const size_t payloadLen = strlen(payload);
  EspNowSend(payload, payloadLen);
}

// report state via esp-now every now and then
void reportViaEspNow(float temperature, int orpMiliVolts)
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

  espNow_temperature(thisDeviceId, temperature);    // send temperature
  espNow_orp(thisDeviceId, orpMiliVolts);           // send orp mV value
}

void showError(Error errorNo)
{
  // show error message on display
  max7219.Clear();

  switch(errorNo) {
    case ERROR_ADS:       max7219.DisplayText("Err AdS", LEFT); break;
    case ERROR_ESP:       max7219.DisplayText("Err ESP", LEFT); break;
    case ERROR_TEMP_IN:   max7219.DisplayText("Err Tin", LEFT); break;
    case ERROR_TEMP_OUT:  max7219.DisplayText("Err Tout", LEFT); break;
    default:              max7219.DisplayText("Err", LEFT); break;
  }
}

void showTempAndOrp(float temperature, int orpMiliVolts)
{
  char buf[16];

  Serial.print("temp: ");
  snprintf(buf, sizeof(buf), "%.1f", temperature);
  Serial.print(buf);

  Serial.print(", orp: ");
  snprintf(buf, sizeof(buf), "%d", orpMiliVolts);
  Serial.println(buf);

  snprintf(buf, 12, "%5.1f%4d", temperature, orpMiliVolts);
  max7219.Clear();
  max7219.DisplayText(buf, LEFT);

  Serial.print("display: '");
  Serial.print(buf);
  Serial.println("'");
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
  float temperature = sensorIn.getTempCByIndex(0);

  if(temperature < -30 || temperature > 70) {     // temp seems wrong? show error for a while, but proceed with the rest
    showError(ERROR_TEMP_IN);
    delay(1000);
  }

  // low-pass filter for stable ORP display
  float multiplier = 0.0625f;           // ADS1115  @ 2x gain +/- 2.048V gain (16-bit results)
  int32_t adcAccum = 0;
  for (int i = 0; i < 5; i++) {
      adcAccum += ads.readADC_Differential_0_1();
      delay(5);
  }
  int16_t results = adcAccum / 5;
  int orpMiliVolts = roundf(results * multiplier);

  // report state via esp-now every now and then
  reportViaEspNow(temperature, orpMiliVolts);

  // update display with temps and action
  showTempAndOrp(temperature, orpMiliVolts);
}

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status)
{
  Serial.print("ESP-NOW send status: ");
  Serial.println((int)status);
  (void)mac_addr; // mac_addr may be unused (broadcast peer)
}
