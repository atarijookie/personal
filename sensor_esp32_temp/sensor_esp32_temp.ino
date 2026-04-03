#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>
#include <string.h>

// ESP-NOW sender configuration
static const int thisDeviceId = 1;
static const uint8_t espNowBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status);

static const uint8_t PIN_TXD = 20;
static const uint8_t PIN_RXD = 21;

// GPIO assignments
static const uint8_t PIN_DONE = 3;        // GPIO3
static const uint8_t PIN_PWROUT_DIV = 0;  // GPIO0 (ADC input)

// I2C pin assignments for AHT20
static const uint8_t I2C_SDA_PIN = 6;     // GPIO6
static const uint8_t I2C_SCL_PIN = 7;     // GPIO7

static Adafruit_AHTX0 s_aht;
static bool s_ahtOk = false;
static float s_lastTempC = 0.0f;
static int s_lastHumidityPct = 0;

uint32_t durationSetup, durationRun;

void setup() {
  uint32_t start = millis();
  Serial.begin(115200);

  Serial.println("Config pins");

  pinMode(PIN_DONE, OUTPUT);
  digitalWrite(PIN_DONE, LOW);

  // Use the internal hardware noise for a better seed
  randomSeed(analogRead(0));

  Serial.println("Config wifi");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(10);

  // Initialize I2C + AHT20
  Serial.println("Config i2c + aht20");

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN, 100000); // Set to 100kHz

  Wire.beginTransmission(0x38);
  uint8_t error = Wire.endTransmission();
  if(error == 0) {
    Serial.println("i2c ok");
  } else {
    Serial.print("i2c fail, error: ");
    Serial.println(error);
  }

  s_ahtOk = s_aht.begin();
  if (!s_ahtOk) {
    Serial.println("AHT20 init failed");
  } else {
    Serial.println("AHT20 init ok");
  }

  if (esp_now_init() != 0) {
    Serial.println("ESP-NOW init failed");
    delay(100);
    digitalWrite(PIN_DONE, HIGH);
    delay(100);
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
    delay(100);
    digitalWrite(PIN_DONE, HIGH);
    delay(100);
    ESP.restart();
  }

  Serial.println("Config DONE!");
  uint32_t end = millis();
  durationSetup = end - start;
}

void loop() {
  uint32_t start = millis();
  Serial.println("main start");

  // Indicate "busy/sending"
  digitalWrite(PIN_DONE, LOW);

  // Read AHT20 + power divider analog *before* constructing/sending the packet.
  if (s_ahtOk) {
    Serial.println("aht20 read");

    sensors_event_t humidityEvent, tempEvent;
    s_aht.getEvent(&humidityEvent, &tempEvent);

    s_lastHumidityPct = (int)(humidityEvent.relative_humidity + 0.5f);
    s_lastTempC = tempEvent.temperature;
  } else {
    Serial.println("aht20 skip");
  }

  analogReadResolution(12);   // esp32-c3 has max adc resulotion of 12 bits (0-4095)
  const uint16_t pwroOutDivAdc = (uint16_t)analogRead(PIN_PWROUT_DIV);
  float batteryVolts = ((float)pwroOutDivAdc / 4095.0f);    // from <0, 4095> to <0, 1.0>
  batteryVolts = batteryVolts * 3.3f * 2.0f;  // from <0, 1.0> to <0, 3.3>, but the input voltage divider inputs just half, so multiply by 2

  Serial.print("adc raw: ");
  Serial.print((int) pwroOutDivAdc);
  Serial.print(", volts: ");
  Serial.println(batteryVolts);

  const uint32_t packetRandomId =
      ((uint32_t)random(0xFFFF) << 16) | (uint32_t)random(0xFFFF);

  Serial.println("create payload");

  // Construct JSON into a null-terminated char buffer, then send the bytes
  char payload[250];

  if(s_ahtOk) {   // temp + humidity read ok
    snprintf(payload, sizeof(payload),
            "{\"type\":\"temp_sensor\",\"dev_id\":%d,\"packet_id\":%08lu,\"temp\":%.1f,\"humidity\":%d,\"battery\":%.2f}\n",
            thisDeviceId,
            (unsigned long)packetRandomId,
            (double)s_lastTempC,
            s_lastHumidityPct,
            (double)batteryVolts);
  } else {  // temp + humidity read failed
    snprintf(payload, sizeof(payload),
            "{\"type\":\"temp_sensor\",\"dev_id\":%d,\"packet_id\":%08lu,\"temp\":null,\"humidity\":null,\"battery\":%.2f}\n",
            thisDeviceId,
            (unsigned long)packetRandomId,
            (double)batteryVolts);
  }

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
    esp_now_send(espNowBroadcastMac,
                  reinterpret_cast<const uint8_t*>(payload),
                  payloadLen);

    // Small visible pacing
    delay(10);
  }

  uint32_t end = millis();
  durationRun = end - start;

  Serial.print("setup + run: ");
  Serial.print(durationSetup);
  Serial.print(" + ");
  Serial.print(durationRun);
  Serial.println(" ms");

  Serial.println("turning off or 5s wait + restart");

  // Indicate "done" after all packets were sent.
  digitalWrite(PIN_DONE, HIGH);   // this should turn off via TPL5110

  delay(5000);  // this should never happen (or not wait whole 5s and restart as the power should be already off)
}

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status)
{
  Serial.print("ESP-NOW send status: ");
  Serial.println((int)status);
  (void)mac_addr; // mac_addr may be unused (broadcast peer)
}
