// PixoTicker replacement firmware: network-driven HUB75 display.
//
// Recovery model:
//   * WiFiManager runs first, with the vendor's AP name "PixoTicker".
//   * A soft AP with the same name stays up after every successful boot.
//   * POST /u accepts a firmware image (same path the vendor portal exposes).
//   * RTC boot counter + 8 s one-shot timer: a panel configuration that hangs
//     increments the counter, and after two resets the panel is skipped
//     entirely so the network path always comes back.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include <WebServer.h>
#include <Preferences.h>
#include <Update.h>
#include <HTTPClient.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>
#include <esp_timer.h>

#include "panel.h"
#include "relay_ca.h"

RTC_NOINIT_ATTR static uint32_t s_magic;
RTC_NOINIT_ATTR static uint32_t s_boots;
RTC_NOINIT_ATTR static uint32_t s_last_rc;  // 1 ok, 2 begin false, 3 guard fired
static const uint32_t BOOT_MAGIC = 0x50585431;  // "PXT1"
static const uint32_t MAX_PANEL_ATTEMPTS = 2;
static const char *AP_NAME = "PixoTicker";

static Preferences s_prefs;
static WebServer s_http(80);
static PanelCfg s_cfg;
static uint16_t *s_fb = nullptr;
static bool s_safe = false;
static uint8_t s_brightness = 128;
static bool s_ota_ok = false;
static bool s_healthy = false;
static uint32_t s_ota_written = 0;
static esp_timer_handle_t s_guard = nullptr;

// Build-time settings live in src/secrets.h, which is gitignored. Copy
// src/secrets.h.example and fill it in; with no host the panel runs offline
// and serves only its own access point.
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef PXT_RELAY_HOST
#define PXT_RELAY_HOST ""
#endif
#ifndef PXT_RELAY_PORT
#define PXT_RELAY_PORT 443
#endif
#ifndef PXT_RELAY_KEY
#define PXT_RELAY_KEY ""
#endif
static volatile uint32_t s_pull_frames = 0;
static volatile uint32_t s_pull_errors = 0;
static volatile uint32_t s_pull_seq = 0;
static volatile int32_t s_pull_code = 0;
static volatile uint32_t s_tls_fails = 0;
static volatile bool s_tls_insecure = false;
static volatile bool s_tls_verified = false;
static char s_tls_err[64] = "";
static volatile uint32_t s_ops_applied = 0;
static char s_pull_dns[24] = "?";
static char s_pull_phase[24] = "idle";
static void set_phase(const char *p) { snprintf(s_pull_phase, sizeof(s_pull_phase), "%s", p); }

// ---------------------------------------------------------------- config

static void loadConfig() {
  s_prefs.begin("pxt", true);
  s_cfg.across = s_prefs.getUChar("across", 2);
  s_cfg.down = s_prefs.getUChar("down", 1);
  s_cfg.serpentine = s_prefs.getUChar("serp", 0);
  s_cfg.invertClock = s_prefs.getUChar("invclk", 1);
  s_cfg.modH = s_prefs.getUChar("modh", 64);
  s_safe = s_prefs.getUChar("safe", 0) != 0;
  s_brightness = s_prefs.getUChar("bright", 128);
  s_prefs.end();
}

static void savePanel(const PanelCfg &c) {
  s_prefs.begin("pxt", false);
  s_prefs.putUChar("across", c.across);
  s_prefs.putUChar("down", c.down);
  s_prefs.putUChar("serp", c.serpentine);
  s_prefs.putUChar("invclk", c.invertClock);
  s_prefs.putUChar("modh", c.modH);
  s_prefs.end();
}

// This panel is wired G/B swapped: a green write lands on the blue LEDs.
static uint16_t rgb565(uint8_t r, uint8_t g, uint8_t b) {
  return (uint16_t)(((r & 0xF8) << 8) | ((b & 0xFC) << 3) | (g >> 3));
}

// ---------------------------------------------------------------- guard

static void guardExpired(void *) {
  s_boots = (s_magic == BOOT_MAGIC) ? s_boots : 1;
  s_magic = BOOT_MAGIC;
  s_boots++;
  s_last_rc = 3;
  esp_restart();
}

static void guardArm(uint32_t micros) {
  const esp_timer_create_args_t args = {.callback = &guardExpired,
                                        .arg = nullptr,
                                        .dispatch_method = ESP_TIMER_TASK,
                                        .name = "panel-guard",
                                        .skip_unhandled_events = true};
  if (esp_timer_create(&args, &s_guard) == ESP_OK)
    esp_timer_start_once(s_guard, micros);
}

static void guardDisarm() {
  if (!s_guard) return;
  esp_timer_stop(s_guard);
  esp_timer_delete(s_guard);
  s_guard = nullptr;
}

// ---------------------------------------------------------------- http

static void sendJson(const String &body) {
  s_http.sendHeader("Access-Control-Allow-Origin", "*");
  s_http.send(200, "application/json", body);
}

static String buildStateJson() {
  uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
  String j = "{";
  j += "\"fw\":\"pxt-0.8\",";
  j += "\"build\":\"" __DATE__ " " __TIME__ "\",";
  j += "\"uptime\":" + String(millis() / 1000) + ",";
  j += "\"width\":" + String(w) + ",\"height\":" + String(h) + ",";
  j += "\"across\":" + String(s_cfg.across) + ",\"down\":" + String(s_cfg.down) + ",";
  j += "\"modh\":" + String(s_cfg.modH) + ",";
  j += "\"serpentine\":" + String(s_cfg.serpentine) + ",";
  j += "\"panel\":" + String(panel_active() ? "true" : "false") + ",";
  j += "\"safe\":" + String(s_safe ? "true" : "false") + ",";
  j += "\"boots\":" + String(s_boots) + ",";
  j += "\"last_rc\":" + String(s_last_rc) + ",";
  j += "\"brightness\":" + String(s_brightness) + ",";
  j += "\"heap\":" + String(ESP.getFreeHeap()) + ",";
  j += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  j += "\"ap\":\"" + WiFi.softAPIP().toString() + "\",";
  j += "\"pull_frames\":" + String(s_pull_frames) + ",";
  j += "\"pull_errors\":" + String(s_pull_errors) + ",";
  j += "\"pull_seq\":" + String(s_pull_seq) + ",";
  j += "\"pull_code\":" + String(s_pull_code) + ",";
  j += "\"pull_dns\":\"" + String(s_pull_dns) + "\",";
  j += "\"pull_phase\":\"" + String(s_pull_phase) + "\",";
  j += "\"wifi\":" + String((int)WiFi.status()) + ",";
  j += "\"tls_err\":\"" + String(s_tls_err) + "\",";
  j += "\"tls_fails\":" + String(s_tls_fails) + ",";
  j += "\"tls_mode\":\"" + String(s_tls_insecure ? "insecure" : "verified") + "\",";
  j += "\"maxblock\":" + String(ESP.getMaxAllocHeap()) + ",";
  j += "\"epoch\":" + String((unsigned long)time(nullptr)) + ",";
  j += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  j += "\"ops\":" + String(s_ops_applied);
  j += "}";
  return j;
}

static void handleState() { sendJson(buildStateJson()); }

// No RTC battery: the clock reads 1970 until SNTP answers, and TLS refuses a
// certificate it cannot place in time.
static bool clockReady() {
  return time(nullptr) > 1700000000;  // 2023-11-14
}

// The panel only dials out. Rather than open a second TLS session just to
// report itself (the handshake needs a contiguous block the pull already
// holds), the state rides the pull request as a header the relay stores.
static String stateHeader() {
  String j = "{\"build\":\"" + String(__DATE__ " " __TIME__) +
             "\",\"uptime\":" + String(millis() / 1000) +
             ",\"heap\":" + String(ESP.getFreeHeap()) +
             ",\"maxblock\":" + String(ESP.getMaxAllocHeap()) +
             ",\"frames\":" + String((uint32_t)s_pull_frames) +
             ",\"errors\":" + String((uint32_t)s_pull_errors) +
             ",\"rssi\":" + String(WiFi.RSSI()) +
             ",\"tls\":\"" + String(s_tls_insecure ? "insecure" : "verified") + "\"}";
  return j;
}

static bool bodyArg(String &out) {
  if (s_http.hasArg("plain")) {
    out = s_http.arg("plain");
    return out.length() > 0;
  }
  return false;
}

static int b64v(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

// Raw bodies cannot carry RGB565: the plain-arg path is NUL terminated, so any
// 0x00 byte truncates the frame. Frames arrive base64 instead.
static size_t b64decode(const String &in, uint8_t *out, size_t cap) {
  size_t n = 0;
  uint32_t acc = 0;
  int bits = 0;
  for (size_t i = 0; i < in.length(); i++) {
    int v = b64v(in[i]);
    if (v < 0) continue;
    acc = (acc << 6) | (uint32_t)v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (n < cap) out[n++] = (uint8_t)((acc >> bits) & 0xFF);
    }
  }
  return n;
}

static void handleFramePost() {
  if (!s_fb) {
    s_http.send(503, "text/plain", "no panel");
    return;
  }
  uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
  String body;
  if (!bodyArg(body)) {
    s_http.send(400, "text/plain", "empty body");
    return;
  }
  String fmt = s_http.arg("fmt");
  bool rgb888 = (fmt == "rgb888");
  size_t need = (size_t)w * h * (rgb888 ? 3 : 2);
  uint8_t *buf = (uint8_t *)malloc(need);
  if (!buf) {
    s_http.send(503, "text/plain", "oom");
    return;
  }
  size_t got = b64decode(body, buf, need);
  if (got < need) {
    free(buf);
    s_http.send(400, "text/plain", "short frame");
    return;
  }
  for (size_t i = 0; i < (size_t)w * h; i++) {
    if (rgb888)
      s_fb[i] = rgb565(buf[i * 3], buf[i * 3 + 1], buf[i * 3 + 2]);
    else
      s_fb[i] = (uint16_t)((buf[i * 2] << 8) | buf[i * 2 + 1]);
  }
  free(buf);
  panel_show(s_fb);
  s_http.send(200, "application/json", "{\"ok\":true}");
}

static void handleFrameGet() {
  uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
  size_t n = (size_t)w * h * 2;
  uint8_t *out = (uint8_t *)malloc(n);
  if (!out) {
    s_http.send(503, "text/plain", "oom");
    return;
  }
  for (size_t i = 0; i < (size_t)w * h; i++) {
    out[i * 2] = s_fb ? (uint8_t)(s_fb[i] >> 8) : 0;
    out[i * 2 + 1] = s_fb ? (uint8_t)(s_fb[i] & 0xFF) : 0;
  }
  s_http.setContentLength(n);
  s_http.send(200, "application/octet-stream", "");
  s_http.sendContent((const char *)out, n);
  free(out);
}

// Body: "x,y,r,g,b" or query x=..&y=..&r=..
static bool parsePixel(int *x, int *y, int *r, int *g, int *b) {
  if (s_http.hasArg("x")) {
    *x = s_http.arg("x").toInt();
    *y = s_http.arg("y").toInt();
    *r = s_http.arg("r").toInt();
    *g = s_http.arg("g").toInt();
    *b = s_http.arg("b").toInt();
    return true;
  }
  String body;
  if (!bodyArg(body)) return false;
  return sscanf(body.c_str(), "%d,%d,%d,%d,%d", x, y, r, g, b) == 5;
}

static void handlePixel() {
  int x, y, r, g, b;
  if (!s_fb || !parsePixel(&x, &y, &r, &g, &b)) {
    s_http.send(400, "text/plain", "need x,y,r,g,b");
    return;
  }
  uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
  if (x < 0 || y < 0 || x >= w || y >= h) {
    s_http.send(400, "text/plain", "out of range");
    return;
  }
  s_fb[(size_t)y * w + x] = rgb565(r, g, b);
  panel_show(s_fb);
  s_http.send(200, "application/json", "{\"ok\":true}");
}

// Body: one "x,y,r,g,b" per line, or separated by ';'.
static void handlePixels() {
  String body;
  if (!s_fb || !bodyArg(body)) {
    s_http.send(400, "text/plain", "empty body");
    return;
  }
  uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
  int n = 0;
  int pos = 0;
  while (pos < (int)body.length()) {
    int x, y, r, g, b;
    int used = 0;
    if (sscanf(body.c_str() + pos, "%d,%d,%d,%d,%d%n", &x, &y, &r, &g, &b, &used) != 5) break;
    if (x >= 0 && y >= 0 && x < w && y < h) s_fb[(size_t)y * w + x] = rgb565(r, g, b);
    n++;
    pos += used;
    while (pos < (int)body.length() && (body[pos] == '\n' || body[pos] == '\r' || body[pos] == ';')) pos++;
  }
  if (n) panel_show(s_fb);
  s_http.send(200, "application/json", "{\"n\":" + String(n) + "}");
}

static void handleBrightness() {
  int level = s_http.hasArg("level") ? s_http.arg("level").toInt() : -1;
  if (level < 0) {
    String body;
    if (bodyArg(body)) level = body.toInt();
  }
  if (level < 0 || level > 255) {
    s_http.send(400, "text/plain", "level 0..255");
    return;
  }
  s_brightness = (uint8_t)level;
  s_prefs.begin("pxt", false);
  s_prefs.putUChar("bright", s_brightness);
  s_prefs.end();
  panel_setBrightness(s_brightness);
  s_http.send(200, "application/json", "{\"brightness\":" + String(level) + "}");
}

static void handlePanelCfg() {
  bool changed = false;
  PanelCfg c = s_cfg;
  if (s_http.hasArg("across")) { c.across = (uint8_t)s_http.arg("across").toInt(); changed = true; }
  if (s_http.hasArg("down")) { c.down = (uint8_t)s_http.arg("down").toInt(); changed = true; }
  if (s_http.hasArg("serp")) { c.serpentine = (uint8_t)s_http.arg("serp").toInt(); changed = true; }
  if (s_http.hasArg("invclk")) { c.invertClock = (uint8_t)s_http.arg("invclk").toInt(); changed = true; }
  if (s_http.hasArg("modh")) { c.modH = (uint8_t)s_http.arg("modh").toInt(); changed = true; }
  if (!changed) {
    sendJson("{\"across\":" + String(s_cfg.across) + ",\"down\":" + String(s_cfg.down) +
             ",\"serp\":" + String(s_cfg.serpentine) + ",\"invclk\":" + String(s_cfg.invertClock) + ",\"modh\":" + String(s_cfg.modH) + "}");
    return;
  }
  if (c.across == 0 || c.down == 0 || c.across * c.down > 4) {
    s_http.send(400, "text/plain", "across*down must be 1..4");
    return;
  }
  savePanel(c);
  s_boots = 0;
  s_last_rc = 0;
  s_http.send(200, "application/json", "{\"ok\":true,\"reboot\":true}");
  delay(300);
  ESP.restart();
}

static void handleBootsReset() {
  s_boots = 0;
  s_last_rc = 0;
  s_http.send(200, "application/json", "{\"boots\":0}");
}

// Try a panel configuration in place, under the same watchdog that protects
// boot. A hang resets the chip and increments the boot counter.
static void handlePanelTry() {
  PanelCfg c = s_cfg;
  if (s_http.hasArg("across")) c.across = (uint8_t)s_http.arg("across").toInt();
  if (s_http.hasArg("down")) c.down = (uint8_t)s_http.arg("down").toInt();
  if (s_http.hasArg("serp")) c.serpentine = (uint8_t)s_http.arg("serp").toInt();
  if (s_http.hasArg("invclk")) c.invertClock = (uint8_t)s_http.arg("invclk").toInt();
  if (s_http.hasArg("modh")) c.modH = (uint8_t)s_http.arg("modh").toInt();
  if (c.across == 0 || c.down == 0 || c.across * c.down > 4) {
    s_http.send(400, "text/plain", "across*down must be 1..4");
    return;
  }
  if (panel_active()) panel_end();
  guardArm(8ULL * 1000 * 1000);
  bool ok = panel_begin(c);
  guardDisarm();
  s_last_rc = ok ? 1 : 2;
  if (ok) {
    s_cfg = c;
    savePanel(c);
    s_boots = 0;
    uint16_t w = panelWidth(c), h = panelHeight(c);
    if (!s_fb) s_fb = (uint16_t *)calloc((size_t)w * h, sizeof(uint16_t));
    panel_setBrightness(s_brightness);
  }
  sendJson("{\"ok\":" + String(ok ? "true" : "false") +
           ",\"across\":" + String(c.across) + ",\"down\":" + String(c.down) +
           ",\"serp\":" + String(c.serpentine) + ",\"invclk\":" + String(c.invertClock) +
           ",\"last_rc\":" + String(s_last_rc) + ",\"heap\":" + String(ESP.getFreeHeap()) + "}");
}
static void handleSafe() {
  bool on = true;
  if (s_http.hasArg("on")) on = s_http.arg("on").toInt() != 0;
  s_prefs.begin("pxt", false);
  s_prefs.putUChar("safe", on ? 1 : 0);
  s_prefs.end();
  s_http.send(200, "application/json", on ? "{\"safe\":true}" : "{\"safe\":false}");
  delay(200);
  ESP.restart();
}

static void handleTest() {
  panel_test_pattern();
  s_http.send(200, "application/json", "{\"ok\":true}");
}

// ---------------------------------------------------------------- OTA

static void handleUpdateDone() {
  Serial.printf("[ota] done ok=%d written=%u error=%d\n", s_ota_ok, s_ota_written, Update.hasError());
  if (!s_ota_ok || Update.hasError()) {
    s_http.send(500, "text/plain", String("update failed: ") + (s_ota_written ? Update.errorString() : "no upload received"));
    return;
  }
  s_http.send(200, "text/plain", "update ok, rebooting");
  delay(800);
  ESP.restart();
}

static void otaChunk(HTTPUpload &up) {
  if (up.status != UPLOAD_FILE_WRITE) Serial.printf("[ota] status=%u total=%u\n", up.status, up.totalSize);
  if (up.status == UPLOAD_FILE_START) {
    s_ota_ok = false;
    s_ota_written = 0;
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) Update.printError(Serial);
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (Update.write(up.buf, up.currentSize) != up.currentSize) Update.printError(Serial);
  } else if (up.status == UPLOAD_FILE_END) {
    s_ota_written = up.totalSize + up.currentSize;
    s_ota_ok = Update.end(true);
    if (!s_ota_ok) Update.printError(Serial);
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    Update.end();
  }
}

// Owns /u so the web server never routes a non-multipart body here: canRaw()
// returns false, and upload() is the only entry point that touches Update.
class OtaHandler : public RequestHandler {
 public:
  bool canHandle(HTTPMethod m, const String &u) override { return m == HTTP_POST && u == "/u"; }
  bool canHandle(WebServer &srv, HTTPMethod m, const String &u) override {
    (void)srv;
    return m == HTTP_POST && u == "/u";
  }
  bool canUpload(const String &u) override { (void)u; return true; }
  bool canUpload(WebServer &srv, const String &u) override { (void)srv; (void)u; return true; }
  bool canRaw(const String &u) override { (void)u; return false; }
  bool canRaw(WebServer &srv, const String &u) override { (void)srv; (void)u; return false; }
  void upload(WebServer &srv, const String &u, HTTPUpload &up) override {
    (void)srv;
    (void)u;
    otaChunk(up);
  }
  bool handle(WebServer &srv, HTTPMethod m, const String &u) override {
    (void)srv;
    (void)m;
    (void)u;
    handleUpdateDone();
    return true;
  }
};

static const char UPDATE_FORM[] PROGMEM =
    "<!doctype html><html><body style='font-family:sans-serif;background:#111;color:#eee'>"
    "<h2>PixoTicker firmware</h2><form method='POST' action='/u' enctype='multipart/form-data'>"
    "<input type='file' name='update' accept='.bin'><button>Update</button></form>"
    "<p><a href='/state' style='color:#4af'>state</a></p></body></html>";

static void handleUpdatePage() { s_http.send_P(200, "text/html", UPDATE_FORM); }

static void handleRoot() {
  String j = "<!doctype html><body style='font-family:sans-serif;background:#111;color:#eee'>"
             "<h2>PixoTicker</h2><ul>"
             "<li><a href='/state' style='color:#4af'>/state</a></li>"
             "<li><a href='/wifi' style='color:#4af'>/wifi</a> (network)</li>"
             "<li><a href='/test' style='color:#4af'>/test</a> (ruler)</li>"
             "<li><a href='/update' style='color:#4af'>/update</a> (OTA)</li>"
             "<li>POST /frame, /pixel, /pixels, /brightness, /panel, /safe</li></ul></body>";
  s_http.send(200, "text/html", j);
}

static void handleWifiPage() {
  int n = WiFi.scanNetworks();
  String page = "<!doctype html><body style='font-family:sans-serif;background:#111;color:#eee'>"
                "<h2>WiFi</h2><ul>";
  for (int i = 0; i < n; i++)
    page += "<li>" + WiFi.SSID(i) + " (" + String(WiFi.RSSI(i)) + " dBm)</li>";
  page += "</ul><form method='POST' action='/wifisave'>"
          "<p>SSID<br><input name='s' maxlength='32'></p>"
          "<p>Password<br><input name='p' type='password' maxlength='64'></p>"
          "<button>Save and reboot</button></form></body>";
  WiFi.scanDelete();
  s_http.send(200, "text/html", page);
}

static void handleWifiSave() {
  String s = s_http.arg("s"), p = s_http.arg("p");
  s_prefs.begin("pxt", false);
  s_prefs.putString("ssid", s);
  s_prefs.putString("pass", p);
  s_prefs.end();
  s_http.send(200, "text/html", "saved, rebooting");
  delay(300);
  ESP.restart();
}

// ---------------------------------------------------------------- setup

static void startNetwork() {
  s_prefs.begin("pxt", true);
  String ssid = s_prefs.getString("ssid", "");
  String pass = s_prefs.getString("pass", "");
  s_prefs.end();

  // AP comes up before anything can block, on every boot.
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_NAME);
  WiFi.setSleep(false);

  if (ssid.length()) {
    WiFi.begin(ssid.c_str(), pass.c_str());
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) delay(250);
  }
  Serial.printf("[net] sta=%s ap=%s\n", WiFi.localIP().toString().c_str(),
                WiFi.softAPIP().toString().c_str());
}


// Panel pulls frames from the relay over the internet: no laptop in the path.
// One persistent connection carries a chunked stream of "PXTF" + seq + 16384B.
static const uint32_t FRAME_MAGIC = 0x50585446u;  // "PXTF"
static const uint32_t RLE_MAGIC = 0x50585452u;    // "PXTR"

static bool readLine(WiFiClient &c, char *out, size_t cap, uint32_t ms) {
  size_t n = 0;
  uint32_t t0 = millis();
  while (millis() - t0 < ms) {
    if (!c.available()) {
      if (!c.connected()) return false;
      delay(1);
      continue;
    }
    int ch = c.read();
    if (ch < 0) continue;
    if (ch == '\n') {
      out[n] = 0;
      return true;
    }
    if (ch != '\r' && n + 1 < cap) out[n++] = (char)ch;
  }
  return false;
}



// ---------------------------------------------------------------- firmware update
// The panel has no inbound port, so a fix ships as a key-authenticated download:
// the op carries the file's size and SHA-256, and nothing is committed unless
// both match.

static String jsonStr(const char *js, const char *key) {
  String pat = String("\"") + key + "\":\"";
  const char *p = strstr(js, pat.c_str());
  if (!p) return String();
  p += pat.length();
  const char *e = strchr(p, '"');
  if (!e) return String();
  return String(p).substring(0, e - p);
}

static bool otaFrom(const String &path, size_t want, const String &shaHex) {
  WiFiClientSecure c;
  c.setCACert(RELAY_CA);
  HTTPClient http;
  http.setTimeout(20000);
  if (!http.begin(c, String("https://") + PXT_RELAY_HOST + path)) return false;
  http.addHeader("X-Api-Key", PXT_RELAY_KEY);
  int code = http.GET();
  if (code != 200) {
    Serial.printf("[ota] http %d\n", code);
    http.end();
    return false;
  }
  int len = http.getSize();
  if (len <= 0 || (size_t)len != want) {
    Serial.printf("[ota] size %d want %u\n", len, (unsigned)want);
    http.end();
    return false;
  }
  if (!Update.begin((size_t)len)) {
    Serial.println("[ota] begin failed");
    http.end();
    return false;
  }
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  WiFiClient *st = http.getStreamPtr();
  uint8_t buf[1024];
  size_t total = 0;
  while (total < (size_t)len) {
    size_t ask = sizeof(buf);
    if ((size_t)len - total < ask) ask = (size_t)len - total;
    size_t r = st->readBytes(buf, ask);
    if (!r) break;
    mbedtls_sha256_update(&ctx, buf, r);
    Update.write(buf, r);
    total += r;
  }
  uint8_t dig[32];
  mbedtls_sha256_finish(&ctx, dig);
  mbedtls_sha256_free(&ctx);
  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", dig[i]);
  bool match = shaHex.equalsIgnoreCase(hex);
  http.end();
  if (total != (size_t)len || !match) {
    Serial.printf("[ota] reject total=%u match=%d\n", (unsigned)total, match);
    Update.abort();
    return false;
  }
  return Update.end(true);
}

// Re-queued ops must not reinstall the image that is already running: that
// would reboot the panel on every reconnect. NVS survives an app flash, so the
// version written after a verified install is the one to compare.
static String s_ota_installed;

static void rememberInstalled(const String &shaHex) {
  s_prefs.begin("pxt", false);
  s_prefs.putString("otapending", shaHex);  // proven only by the first frame
  s_prefs.end();
}

static void promoteInstalled() {
  s_prefs.begin("pxt", false);
  String pending = s_prefs.getString("otapending", "");
  if (pending.length()) {
    s_prefs.putString("otasha", pending);
    s_prefs.remove("otapending");
    s_ota_installed = pending;
  }
  s_prefs.end();
}

static void otaOp(const char *js) {
  String path = jsonStr(js, "path");
  String sha = jsonStr(js, "sha256");
  const char *sz = strstr(js, "\"size\":");
  size_t want = sz ? (size_t)atol(sz + 7) : 0;
  if (!path.length() || sha.length() != 64 || !want) {
    Serial.println("[ota] bad op");
    return;
  }
  if (!s_ota_installed.length()) {
    s_prefs.begin("pxt", true);
    s_ota_installed = s_prefs.getString("otasha", "");
    s_prefs.end();
  }
  if (sha.equalsIgnoreCase(s_ota_installed)) {
    Serial.println("[ota] already running this image");
    return;
  }
  Serial.printf("[ota] %s %u\n", path.c_str(), (unsigned)want);
  if (!otaFrom(path, want, sha)) return;
  rememberInstalled(sha);
  Serial.println("[ota] written, restarting");
  delay(300);
  ESP.restart();
}

static const uint32_t OP_MAGIC = 0x5058544Fu;  // "PXTO"

static void applyOp(const char *js) {
  s_ops_applied++;
  Serial.printf("[op] %s\n", js);
  // An unverified session cannot be trusted with anything that changes the
  // device: an on-path attacker could install an image of their choosing.
  if (!s_tls_verified && (strstr(js, "\"ota\"") || strstr(js, "\"reboot\""))) {
    Serial.println("[op] refused: session is not certificate-verified");
    return;
  }
  if (strstr(js, "\"reboot\"")) {
    delay(300);
    ESP.restart();
    return;
  }
  if (strstr(js, "\"ota\"")) {
    otaOp(js);
    return;
  }
  const char *b = strstr(js, "\"brightness\"");
  if (b) {
    const char *v = strstr(b, "\"value\":");
    if (v) {
      int lvl = atoi(v + 8);
      if (lvl < 0) lvl = 0;
      if (lvl > 255) lvl = 255;
      s_brightness = (uint8_t)lvl;
      s_prefs.begin("pxt", false);
      s_prefs.putUChar("bright", s_brightness);
      s_prefs.end();
      panel_setBrightness(s_brightness);
    }
  }
}

struct FrameSink {
  // Frames are written straight into the display buffer, two bytes per pixel.
  // No staging block, so the TLS handshake is never competing for 16 KB.
  uint32_t win = 0, seq = 0, frames = 0;
  uint8_t seqb[4];
  int need_seq = 0;
  size_t got = 0;
  int mode = 0;  // 0 scan, 1 frame, 2 op, 4 run-encoded frame
  uint8_t opbuf[224];
  uint8_t opn = 0, opneed = 0, skipn = 0;
  uint16_t runpix = 0;   // pixel index while decoding runs
  uint8_t rphi = 0, rplo = 0, rpphase = 0;

  void finish() {
    panel_show(s_fb);
    frames++;
    if (!s_healthy) {
      s_healthy = true;
      esp_ota_mark_app_valid_cancel_rollback();
      promoteInstalled();
      Serial.println("[ota] image confirmed");
    }
    mode = 0;
    win = 0;
  }

  void feed(uint8_t b) {
    if (mode == 3) {
      if (--skipn == 0) {
        mode = 0;
        win = 0;
      }
      return;
    }
    if (mode == 2) {
      if (opneed == 0) {
        opneed = b;
        opn = 0;
        if (opneed == 0) {
          mode = 0;
          win = 0;
        } else if (opneed > sizeof(opbuf) - 1) {
          skipn = (uint8_t)(opneed - 1);  // drop the body, keep the stream aligned
          mode = 3;
          win = 0;
        }
        return;
      }
      opbuf[opn++] = b;
      if (opn >= opneed) {
        opbuf[opneed < sizeof(opbuf) ? opneed : sizeof(opbuf) - 1] = 0;
        applyOp((const char *)opbuf);
        mode = 0;
        win = 0;
      }
      return;
    }
    if (mode == 4) {
      // hi, lo, count: the panel decodes runs instead of raw pixels because
      // its receive window will not carry 16 KB per frame at stream rate.
      if (rpphase == 0) {
        rphi = b;
        rpphase = 1;
      } else if (rpphase == 1) {
        rplo = b;
        rpphase = 2;
      } else {
        rpphase = 0;
        uint16_t v = ((uint16_t)rphi << 8) | rplo;
        for (uint8_t k = 0; k < b && runpix < 8192; k++) s_fb[runpix++] = v;
        if (runpix >= 8192) finish();
      }
      return;
    }
    if (mode == 0) {
      win = ((win << 8) | b) & 0xFFFFFFFFu;
      if (win == FRAME_MAGIC) {
        mode = 1;
        need_seq = 4;
        win = 0;
      } else if (win == RLE_MAGIC) {
        mode = 4;
        need_seq = 4;
        win = 0;
      } else if (win == OP_MAGIC) {
        mode = 2;
        opneed = 0;
        opn = 0;
        win = 0;
      }
      return;
    }
    if (need_seq > 0) {
      seqb[4 - need_seq] = b;
      if (--need_seq == 0) {
        seq = ((uint32_t)seqb[0] << 24) | ((uint32_t)seqb[1] << 16) |
              ((uint32_t)seqb[2] << 8) | seqb[3];
        got = 0;
        runpix = 0;
        rpphase = 0;
      }
      return;
    }
    if (got & 1)
      s_fb[got >> 1] = (uint16_t)((s_fb[got >> 1] & 0xFF00) | b);
    else
      s_fb[got >> 1] = (uint16_t)(((uint16_t)b << 8) | (s_fb[got >> 1] & 0xFF));
    got++;
    if (got == 16384) finish();
  }
};

static void pullTask(void *) {
  if (!strlen(PXT_RELAY_KEY) || !strlen(PXT_RELAY_HOST)) {
    Serial.println("[pull] disabled, no relay configured");
    vTaskDelete(nullptr);
    return;
  }

  Serial.printf("[pull] %s /pxt/stream\n", PXT_RELAY_HOST);
  for (;;) {
    set_phase("clock");
    if (!clockReady()) {
      delay(500);
      continue;
    }
    set_phase(WiFi.status() != WL_CONNECTED ? "wifi" : (!s_fb ? "fb" : "connect"));
    if (WiFi.status() != WL_CONNECTED || !s_fb) {
      delay(1000);
      continue;
    }
    WiFiClientSecure sc;
    if (s_tls_insecure) sc.setInsecure();  // still encrypted, chain unverified
    else sc.setCACert(RELAY_CA);
    sc.setTimeout(15);
    WiFiClient &c = sc;
    s_tls_verified = false;
    // Resolve separately so /state says whether DNS or the handshake is at fault.
    set_phase("dns");
    IPAddress rip;
    int drc = WiFi.hostByName(PXT_RELAY_HOST, rip);
    snprintf(s_pull_dns, sizeof(s_pull_dns), "%s", drc == 1 ? rip.toString().c_str() : "fail");
    if (!c.connect(PXT_RELAY_HOST, PXT_RELAY_PORT)) {
      s_pull_errors++;
      s_pull_code = -1;
      char err[64] = "";
      int rc = sc.lastError(err, sizeof(err));
      snprintf(s_tls_err, sizeof(s_tls_err), "%d %s", rc, err);
      s_tls_fails = s_tls_fails + 1;
      if (!s_tls_insecure && s_tls_fails >= 5) {
        // Keep the display alive while the chain or clock is sorted out; the
        // key still travels inside TLS.
        s_tls_insecure = true;
        Serial.println("[pull] verification failed five times, unverified TLS");
      }
      delay(3000);
      continue;
    }
    s_tls_verified = !s_tls_insecure;
    set_phase("request");
    String req = String("GET /pxt/stream?enc=rle&since=") + String(s_pull_seq) +
                 " HTTP/1.1\r\nHost: " + PXT_RELAY_HOST + "\r\nX-Api-Key: " PXT_RELAY_KEY +
                 "\r\nX-Pxt-State: " + stateHeader() +
                 "\r\nConnection: close\r\n\r\n";
    c.print(req);
    char line[160];
    int status = 0;
    if (readLine(c, line, sizeof(line), 10000)) sscanf(line, "HTTP/%*s %d", &status);
    while (readLine(c, line, sizeof(line), 10000)) {
      if (!line[0]) break;
    }
    s_pull_code = status;
    if (status != 200) {
      c.stop();
      s_pull_errors++;
      delay(3000);
      continue;
    }
    set_phase("stream");
    FrameSink sink;
    // One bulk read per socket wake-up. Reading the chunk headers a byte at a
    // time on a TLS client left the receive window closed and the panel a
    // backlog behind the renderer.
    uint8_t rx[4096];
    char hex[16];
    int hexn = 0;
    long left = 0;          // payload bytes still owed by the current chunk
    bool in_payload = false;
    uint32_t idle = millis();
    for (;;) {
      int r = c.read(rx, sizeof(rx));
      if (r <= 0) {
        if (!c.connected()) break;
        if (millis() - idle > 90000) break;
        delay(1);
        continue;
      }
      idle = millis();
      for (int i = 0; i < r; i++) {
        uint8_t b = rx[i];
        if (!in_payload) {
          if (b == '\n') {
            hex[hexn < (int)sizeof(hex) - 1 ? hexn : (int)sizeof(hex) - 1] = 0;
            long n = strtol(hex, nullptr, 16);
            hexn = 0;
            if (n <= 0) goto stream_end;
            left = n;
            in_payload = true;
          } else if (b != '\r' && hexn < (int)sizeof(hex) - 1) {
            hex[hexn++] = (char)b;
          }
          continue;
        }
        sink.feed(b);
        if (--left == 0) {
          in_payload = false;  // the trailing CRLF is skipped by the header state
        }
      }
      if (sink.frames) {
        s_pull_frames = sink.frames;
        s_pull_seq = sink.seq;
      }
    }
  stream_end:
    if (sink.frames) {
      s_pull_frames = sink.frames;
      s_pull_seq = sink.seq;
    }

    c.stop();
    delay(300);
  }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[pxt] boot");

  // A new image stays on trial until a frame actually arrives: an app that
  // boots but never pulls rolls back instead of stranding the panel.

  if (s_magic != BOOT_MAGIC) {
    s_magic = BOOT_MAGIC;
    s_boots = 0;
    s_last_rc = 0;
  }
  s_boots++;

  loadConfig();
  if (s_boots > MAX_PANEL_ATTEMPTS) {
    Serial.printf("[pxt] safe mode: %u boot attempts\n", (unsigned)s_boots);
    s_safe = true;
  }

  startNetwork();
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  if (!s_safe) {
#ifdef PXT_PANEL_DISABLED
    Serial.println("[pxt] panel compiled out (safe image)");
#else
    guardArm(8ULL * 1000 * 1000);
    bool ok = panel_begin(s_cfg);
    guardDisarm();
    if (ok) {
      s_boots = 0;
      uint16_t w = panelWidth(s_cfg), h = panelHeight(s_cfg);
      s_fb = (uint16_t *)calloc((size_t)w * h, sizeof(uint16_t));
      panel_setBrightness(s_brightness);
    } else {
      Serial.println("[pxt] panel begin failed, network only");
    }
#endif
  }

  if (strlen(PXT_RELAY_KEY) && strlen(PXT_RELAY_HOST)) {
    xTaskCreatePinnedToCore(pullTask, "pull", 8192, nullptr, 1, nullptr, 0);
  }

  s_http.on("/", HTTP_GET, handleRoot);
  s_http.on("/wifi", HTTP_GET, handleWifiPage);
  s_http.on("/wifisave", HTTP_POST, handleWifiSave);
  s_http.on("/state", HTTP_GET, handleState);
  s_http.on("/test", HTTP_GET, handleTest);
  s_http.on("/frame", HTTP_POST, handleFramePost);
  s_http.on("/frame", HTTP_GET, handleFrameGet);
  s_http.on("/pixel", HTTP_POST, handlePixel);
  s_http.on("/pixels", HTTP_POST, handlePixels);
  s_http.on("/brightness", HTTP_POST, handleBrightness);
  s_http.on("/panel", HTTP_POST, handlePanelCfg);
  s_http.on("/panel/try", HTTP_POST, handlePanelTry);
  s_http.on("/boots", HTTP_POST, handleBootsReset);
  s_http.on("/safe", HTTP_POST, handleSafe);
  s_http.on("/update", HTTP_GET, handleUpdatePage);
  s_http.addHandler(new OtaHandler());
  s_http.on("/reboot", HTTP_GET, []() {
    s_http.send(200, "text/plain", "rebooting");
    delay(300);
    ESP.restart();
  });
  s_http.onNotFound([]() {
    if (s_http.method() == HTTP_OPTIONS) {
      s_http.sendHeader("Access-Control-Allow-Origin", "*");
      s_http.sendHeader("Access-Control-Allow-Methods", "GET,POST");
      s_http.sendHeader("Access-Control-Allow-Headers", "Content-Type");
      s_http.send(204);
      return;
    }
    s_http.send(404, "text/plain", "not found");
  });
  s_http.begin();
  Serial.printf("[pxt] http up, heap=%u, boots=%u, panel=%d\n", ESP.getFreeHeap(),
                (unsigned)s_boots, panel_active());
}

// ---------------------------------------------------------------- serial cli

static void serialLine(String line) {
  line.trim();
  if (!line.length()) return;
  if (line == "state") {
    Serial.printf("{\"w\":%u,\"h\":%u,\"across\":%u,\"down\":%u,\"serp\":%u,\"invclk\":%u,\"modh\":%u,"
                  "\"panel\":%d,\"safe\":%d,\"boots\":%u,\"rc\":%u,\"heap\":%u}\n",
                  panelWidth(s_cfg), panelHeight(s_cfg), s_cfg.across, s_cfg.down,
                  s_cfg.serpentine, s_cfg.invertClock, s_cfg.modH, panel_active(), s_safe,
                  (unsigned)s_boots, (unsigned)s_last_rc, ESP.getFreeHeap());
  } else if (line == "test") {
    panel_test_pattern();
    Serial.println("test pattern drawn");
  } else if (line == "reboot") {
    Serial.println("rebooting");
    delay(200);
    ESP.restart();
  } else if (line.startsWith("bright ")) {
    int v = line.substring(7).toInt();
    s_brightness = (uint8_t)constrain(v, 0, 255);
    s_prefs.begin("pxt", false);
    s_prefs.putUChar("bright", s_brightness);
    s_prefs.end();
    panel_setBrightness(s_brightness);
    Serial.printf("brightness %u\n", s_brightness);
  } else if (line.startsWith("panel ")) {
    int a = 0, d = 0, serp = 0, inv = 1, mh = 64;
    if (sscanf(line.c_str() + 6, "%d %d %d %d %d", &a, &d, &serp, &inv, &mh) >= 2) {
      PanelCfg c = {(uint8_t)a, (uint8_t)d, (uint8_t)serp, (uint8_t)inv, (uint8_t)mh};
      if (a < 1 || d < 1 || a * d > 4) {
        Serial.println("bad geometry");
        return;
      }
      if (panel_active()) panel_end();
      guardArm(8ULL * 1000 * 1000);
      bool ok = panel_begin(c);
      guardDisarm();
      s_last_rc = ok ? 1 : 2;
      Serial.printf("panel begin %s for %dx%d\n", ok ? "ok" : "failed", a, d);
      if (ok) {
        s_cfg = c;
        savePanel(c);
        s_boots = 0;
        uint16_t w = panelWidth(c), h = panelHeight(c);
        if (!s_fb) s_fb = (uint16_t *)calloc((size_t)w * h, sizeof(uint16_t));
        panel_setBrightness(s_brightness);
        panel_test_pattern();
      }
    } else {
      Serial.println("usage: panel across down [serp] [invclk]");
    }
  } else if (line.startsWith("wifi ")) {
    int sp = line.indexOf(' ', 5);
    if (sp < 0) {
      Serial.println("usage: wifi <ssid> <pass>");
      return;
    }
    String ssid = line.substring(5, sp), pass = line.substring(sp + 1);
    s_prefs.begin("pxt", false);
    s_prefs.putString("ssid", ssid);
    s_prefs.putString("pass", pass);
    s_prefs.end();
    Serial.printf("wifi saved for %s, rebooting\n", ssid.c_str());
    delay(200);
    ESP.restart();
  } else {
    Serial.println("cmds: state | test | bright N | panel A D [serp] [inv] | wifi SSID PASS | reboot");
  }
}

static void pollSerial() {
  static String line;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      serialLine(line);
      line = "";
    } else if (c != '\r' && line.length() < 120) {
      line += c;
    }
  }
}

void loop() {
  s_http.handleClient();
  pollSerial();
}
