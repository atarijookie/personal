#include <ESP8266WiFi.h>

const char* ssid     = "WIFI_SSID";
const char* password = "WIFI_PASSWD";
const char* host     = "192.168.123.55";
const uint16_t port  = 22222;

const char* thisDeviceId = "0000";

void ledToggle(void)
{
  static int isOn = 0;
  isOn = !isOn;
  if(isOn) {
    digitalWrite(LED_BUILTIN, HIGH);
  } else {
    digitalWrite(LED_BUILTIN, LOW);
  }
}

void setup() {
  // put your setup code here, to run once:
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);

  // Use the internal hardware noise for a better seed
  randomSeed(analogRead(0));
}

void loop() {
  Serial.println("S");

  ledToggle();

  // 1. Connect to WiFi
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.println("C");
    ledToggle();
  }

  Serial.println("T");

  // 2. Connect to the server
  WiFiClient client;
  if (client.connect(host, port)) {
    float temp = 22.5;

    uint32_t packetRandomId = random(4294967295);

    char buffer[512];
    snprintf(buffer, sizeof(buffer), "{\"type\": \"temp_sensor\", \"dev_id\": \"%s\", \"packet_id\": %08lu, \"temp\": %.1f}\n", thisDeviceId, packetRandomId, temp);

    // 3. Send short string
    client.print(buffer);

    // 4. Disconnect
    client.stop();
  }

  ledToggle();
  Serial.println("W");

  // 5. Wait 5 seconds
  delay(1000);

  ledToggle();
  // 6. Restart ESP completely
  ESP.restart();
}
