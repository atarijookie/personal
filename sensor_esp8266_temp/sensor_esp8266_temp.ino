#include <ESP8266WiFi.h>
#include <espnow.h>
#include <Wire.h>
#include <Adafruit_AHTX0.h>

extern "C" {
#include "user_interface.h" // wifi_set_channel()
}

// ESP-NOW sender configuration
static const int thisDeviceId = 0;
static const uint8_t espNowBroadcastMac[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

static void onDataSent(uint8_t* mac_addr, uint8_t status);

static const uint8_t PIN_TXD = 1;
static const uint8_t PIN_RXD = 3;

// GPIO assignments
static const uint8_t PIN_DONE = 12;
static const uint8_t PIN_PWROUT_DIV = 0;  // ADC0 / A0

// I2C pin assignments for AHT20
static const uint8_t I2C_SDA_PIN = 4;
static const uint8_t I2C_SCL_PIN = 5;

static const uint8_t PIN_LED = LED_BUILTIN;
static Adafruit_AHTX0 s_aht;
static bool s_ahtOk = false;
static float s_lastTempC = 0.0f;
static int s_lastHumidityPct = 0;

uint32_t durationSetup, durationRun;

void ledToggle(void)
{
  static int isOn = 0;
  isOn = !isOn;
  if (PIN_LED == PIN_DONE) {
    // Avoid fighting with PIN_DONE when a board defines LED_BUILTIN as GPIO2.
    return;
  }
  digitalWrite(PIN_LED, isOn ? HIGH : LOW);
}

void setup() {
  uint32_t start = millis();

  Serial.begin(115200);
  Serial.print("\n\npins: ");

  // uint8_t pins[7] = {PIN_LED, PIN_DONE, PIN_PWROUT_DIV, I2C_SDA_PIN, I2C_SCL_PIN, PIN_TXD, PIN_RXD};
  // for(int i=0; i<7; i++) {
  //   Serial.print(pins[i]); Serial.print(" ");
  // }
  // Serial.println(" ");

  Serial.println("Config pins");
  pinMode(PIN_DONE, OUTPUT);
  digitalWrite(PIN_DONE, LOW);

  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);    // LED on

  // Use the internal hardware noise for a better seed
  randomSeed(analogRead(0));

  Serial.println("Config wifi");
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(10);

  // Initialize I2C + AHT20
  Serial.println("Config i2c + aht20");
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  s_ahtOk = s_aht.begin();
  if (!s_ahtOk) {
    Serial.println("AHT20 init failed");
  } else {
    Serial.println("AHT20 init ok");
  }

  Serial.println("Config ESP-NOW");
  if (esp_now_init() != 0) {
    Serial.println("ESP-NOW init failed");
    delay(100);
    digitalWrite(PIN_DONE, HIGH);
    delay(100);
    ESP.restart();
  }

  // Initialize ESP-NOW as a sender/controller
  esp_now_set_self_role(ESP_NOW_ROLE_CONTROLLER);

  esp_now_register_send_cb(onDataSent);

  // Register broadcast peer so esp_now_send() can be called with FF:FF:FF:FF:FF:FF
  esp_now_add_peer(const_cast<uint8_t*>(espNowBroadcastMac), ESP_NOW_ROLE_CONTROLLER, 0, NULL, 0);

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

  const uint16_t pwroOutDivAdc = (uint16_t)analogRead(PIN_PWROUT_DIV);
  // ADC counts are linear. The 10k+10k divider makes Vout = Vsupply/2, so:
  // Divider voltage: 0..2.5V  =>  Supply voltage: 0..5.0V
  const float batteryVolts = ((float)pwroOutDivAdc / 1023.0f) * 5.0f;

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
  ledToggle();

  // Send the same ESP-NOW broadcast on the requested WiFi channels.
  const uint8_t channels[3] = {1, 6, 11};
  for (int i = 0; i < 3; i++) {
    const uint8_t ch = channels[i];
    wifi_set_channel(ch);
    // Ensure the peer uses the same channel we just switched to.
    esp_now_set_peer_channel(const_cast<uint8_t*>(espNowBroadcastMac), (uint8_t)ch);
    delay(20); // give the radio a moment to switch channels

    // Broadcast MAC
    esp_now_send(const_cast<uint8_t*>(espNowBroadcastMac),
                  reinterpret_cast<uint8_t*>(payload),
                  (int)payloadLen);

    // Small visible pacing
    delay(10);
  }

  digitalWrite(PIN_LED, HIGH);    // LED off

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
  digitalWrite(PIN_LED, HIGH);

  delay(5000);  // this should never happen (or not wait whole 5s and restart as the power should be already off)
}

static void onDataSent(uint8_t* mac_addr, uint8_t status)
{
  (void)mac_addr;
  Serial.print("ESP-NOW send status: ");
  Serial.println(status);
}
