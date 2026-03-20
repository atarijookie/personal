#include <WiFi.h>
#include <esp_now.h>    // esp_now.h for esp32
#include <cstring>

const char* ssid = "WIFI_SSID";
const char* password = "WIFI_PASSWD";

const char* host = "192.168.123.55";
const uint16_t port = 22222;

// Arduino auto-prototype generation can place function prototypes before this
// struct definition; a forward declaration avoids "not declared in this scope".
struct PacketQueueItem;

static void ledToggle(void) {
  static int isOn = 0;
  isOn = !isOn;
  // digitalWrite(LED_BUILTIN, isOn ? HIGH : LOW);
}

static constexpr uint8_t QUEUE_DEPTH = 16;

struct PacketQueueItem {
  int len = 0;
  uint8_t data[ESP_NOW_MAX_DATA_LEN];
};

static PacketQueueItem s_queue[QUEUE_DEPTH];
static volatile uint8_t s_qHead = 0;
static volatile uint8_t s_qTail = 0;
static volatile uint8_t s_qCount = 0;

static void onDataRecv(const esp_now_recv_info* /*info*/, const uint8_t* incomingData, int len) {
  if (len <= 0) {
    return;
  }
  if (len > ESP_NOW_MAX_DATA_LEN) {
    len = ESP_NOW_MAX_DATA_LEN;
  }

  noInterrupts();

  // If the queue is full, overwrite the oldest queued packet.
  if (s_qCount == QUEUE_DEPTH) {
    s_qTail = (s_qTail + 1) % QUEUE_DEPTH;
    s_qCount--;
  }

  PacketQueueItem* item = &s_queue[s_qHead];
  item->len = static_cast<int>(len);
  memcpy(item->data, incomingData, static_cast<size_t>(len));

  s_qHead = (s_qHead + 1) % QUEUE_DEPTH;
  s_qCount++;

  interrupts();
}

static bool popNextPacket(PacketQueueItem& out) {
  bool hasPacket = false;

  noInterrupts();
  if (s_qCount > 0) {
    const PacketQueueItem& item = s_queue[s_qTail];
    out.len = item.len;
    memcpy(out.data, item.data, out.len);

    s_qTail = (s_qTail + 1) % QUEUE_DEPTH;
    s_qCount--;
    hasPacket = true;
  }
  interrupts();

  return hasPacket;
}

void setup() {
  // pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  delay(50);

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  Serial.printf("Connecting WiFi SSID=%s\n", ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
    ledToggle();
  }
  Serial.println();
  Serial.print("WiFi connected, IP=");
  Serial.println(WiFi.localIP());

  Serial.println("Initializing ESP-NOW (receiver/slave)...");
  if (esp_now_init() != 0) {
    Serial.println("ESP-NOW init failed!");
  }

#if defined(ESP_NOW_ROLE_SLAVE)
  esp_now_set_self_role(ESP_NOW_ROLE_SLAVE);
#endif
  esp_now_register_recv_cb(onDataRecv);

  Serial.println("Ready: waiting for ESP-NOW packets.");
}

void loop() {
  // Keep WiFi up (without blocking too long), while ESP-NOW receives via callback.
  static uint32_t lastReconnectAttemptMs = 0;
  if (WiFi.status() != WL_CONNECTED) {
    const uint32_t now = millis();
    if (now - lastReconnectAttemptMs > 5000) {
      lastReconnectAttemptMs = now;
      Serial.println("WiFi disconnected; reconnecting...");
      WiFi.reconnect();
    }
  }

  PacketQueueItem item;
  if (!popNextPacket(item)) {
    delay(2);
    return;
  }

  // digitalWrite(LED_BUILTIN, HIGH);

  // For each ESP-NOW packet:
  // - connect TCP to host:port
  // - write the full received payload bytes
  // - close TCP (WiFi stays connected; ESP-NOW can receive next packet)
  WiFiClient client;
  if (client.connect(host, port)) {
    size_t offset = 0;
    while (offset < item.len) {
      const size_t remaining = item.len - offset;
      const size_t n = client.write(item.data + offset, remaining);
      if (n == 0) break; // avoid tight loop if send buffer is full
      offset += n;
    }
    client.flush();
  }
  client.stop();

  // digitalWrite(LED_BUILTIN, LOW);

  ledToggle();
}
