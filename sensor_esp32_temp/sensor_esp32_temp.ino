#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>
#include <string.h>

// ESP-NOW sender configuration
static const int thisDeviceId = 0;
static const uint8_t espNowBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status);

// GPIO assignments (per your request)
static const uint8_t PIN_DONE = 3;        // GPIO3
static const uint8_t PIN_PWROUT_DIV = 0;  // GPIO0 (ADC input)

// I2C pin assignments for AHT20 (per your request)
static const uint8_t I2C_SDA_PIN = 6; // GPIO6
static const uint8_t I2C_SCL_PIN = 7; // GPIO7

static const uint8_t PIN_LED = LED_BUILTIN;
static Adafruit_AHTX0 s_aht;
static bool s_ahtOk = false;
static float s_lastTempC = 0.0f;
static int s_lastHumidityPct = 0;

void setup() {
  pinMode(PIN_DONE, OUTPUT);
  digitalWrite(PIN_DONE, LOW);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);

  Serial.begin(115200);

  // Use the internal hardware noise for a better seed
  randomSeed(analogRead(0));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(10);

  // Initialize I2C + AHT20
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
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
}

void loop() {
  // Indicate "busy/sending" on GPIO2
  digitalWrite(PIN_DONE, LOW);

  // Read AHT20 + power divider analog *before* constructing/sending the packet.
  if (s_ahtOk) {
    sensors_event_t humidityEvent, tempEvent;
    s_aht.getEvent(&humidityEvent, &tempEvent);

    s_lastHumidityPct = (int)(humidityEvent.relative_humidity + 0.5f);
    s_lastTempC = tempEvent.temperature;
  }

  const uint16_t pwroOutDivAdc = (uint16_t)analogRead(PIN_PWROUT_DIV);
  // ADC counts are linear. The 10k+10k divider makes Vout = Vsupply/2, so:
  // Divider voltage: 0..2.5V  =>  Supply voltage: 0..5.0V
  const float batteryVolts = ((float)pwroOutDivAdc / 1023.0f) * 5.0f;

  const uint32_t packetRandomId =
      ((uint32_t)random(0xFFFF) << 16) | (uint32_t)random(0xFFFF);

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

  Serial.println("SEND");
  
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

  // Indicate "done" after all packets were sent.
  digitalWrite(PIN_DONE, HIGH);   // this should turn off via TPL5110
  digitalWrite(PIN_LED, LOW);

  delay(5000);  // this should never happen (or not wait whole 5s and restart as the power should be already off)
}

static void onDataSent(const uint8_t* mac_addr, esp_now_send_status_t status)
{
  Serial.print("ESP-NOW send status: ");
  Serial.println((int)status);
  (void)mac_addr; // mac_addr may be unused (broadcast peer)
}
