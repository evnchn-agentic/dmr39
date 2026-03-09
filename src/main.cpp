// === DMR-39 IR Web Remote ===
// GPIO3=IR RX, GPIO0=IR TX (demodulated, no carrier), GPIO1=STB VCC
// WiFi web server serves remote control UI
// IRremoteESP8266 for receive/decode, raw GPIO for TX
// ArduinoOTA for secure over-the-air updates

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoOTA.h>
#include <IRrecv.h>
#include <IRremoteESP8266.h>
#include <IRtext.h>
#include <IRutils.h>
#include "wifi_credentials.h"

#define IR_RX_PIN  3
#define IR_TX_PIN  0
#define STB_VCC    1

const uint16_t kCaptureBufferSize = 1024;
const uint8_t kTimeout = 15;
IRrecv irrecv(IR_RX_PIN, kCaptureBufferSize, kTimeout, true);
decode_results results;
WebServer server(80);

static bool autoForward = true;
static volatile bool replaying = false;
static int captureNum = 0;
static uint16_t lastRaw[256];
static uint16_t lastRawLen = 0;
static decode_type_t lastProto = decode_type_t::UNKNOWN;
static uint64_t lastValue = 0;

// === NEC TX on demodulated line (mark=LOW, space=HIGH) ===
static void sendNEC(uint32_t code) {
    replaying = true;
    irrecv.disableIRIn();
    noInterrupts();

    // Leader: 9ms mark + 4.5ms space
    GPIO.out_w1tc.val = (1 << IR_TX_PIN); delayMicroseconds(9000);
    GPIO.out_w1ts.val = (1 << IR_TX_PIN); delayMicroseconds(4500);
    // 32 bits MSB first (library stores NEC codes MSB-first)
    for (int i = 31; i >= 0; i--) {
        GPIO.out_w1tc.val = (1 << IR_TX_PIN); delayMicroseconds(560);
        GPIO.out_w1ts.val = (1 << IR_TX_PIN);
        delayMicroseconds((code & (1UL << i)) ? 1690 : 560);
    }
    // Trailing mark
    GPIO.out_w1tc.val = (1 << IR_TX_PIN); delayMicroseconds(560);
    GPIO.out_w1ts.val = (1 << IR_TX_PIN); // idle

    interrupts();
    delayMicroseconds(1000);
    replaying = false;
    irrecv.enableIRIn();
}

// === Replay raw timing ===
static void replayRaw(uint16_t* data, uint16_t len) {
    if (len < 2) return;
    replaying = true;
    irrecv.disableIRIn();
    noInterrupts();
    for (uint16_t i = 0; i < len; i++) {
        if (i & 1) GPIO.out_w1ts.val = (1 << IR_TX_PIN);
        else       GPIO.out_w1tc.val = (1 << IR_TX_PIN);
        delayMicroseconds(data[i]);
    }
    GPIO.out_w1ts.val = (1 << IR_TX_PIN);
    interrupts();
    delayMicroseconds(1000);
    replaying = false;
    irrecv.enableIRIn();
}

// === Web page ===
static const char PAGE[] PROGMEM = R"rawliteral(<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>DMR-39</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a1a;display:flex;justify-content:center;font-family:system-ui,sans-serif;touch-action:manipulation;min-height:100vh;min-height:100dvh}
.r{background:#222;border-radius:20px;padding:10px;max-width:240px;width:100%;margin:8px auto}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px;margin:3px 0}
.g4{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:4px;margin:3px 0}
.b{background:#3a3a3a;border:1px solid #555;border-radius:8px;color:#ccc;font-size:10px;padding:8px 2px;cursor:pointer;text-align:center;user-select:none;-webkit-tap-highlight-color:transparent;min-height:38px;display:flex;align-items:center;justify-content:center}
.b:active{background:#666!important}
.pw{background:#511;color:#f99}
.mt{color:#f99}
.ok{background:#555;border-radius:50%;font-weight:bold;font-size:13px;min-height:44px}
.dp{font-size:16px}
.n{border-radius:50%;font-size:14px;font-weight:bold;min-height:40px}
.rd{background:#922;color:#fff}.gn{background:#292;color:#fff}
.yl{background:#992;color:#fff}.bl{background:#229;color:#fff}
.x{visibility:hidden}
.sep{border-top:1px solid #333;margin:6px 0}
#L{color:#0a0;font-size:10px;text-align:center;padding:4px;min-height:16px}
.ti{color:#555;font-size:9px;text-align:center;padding:2px}
</style></head><body>
<div class="r">
<div class="g3">
 <div class="b pw" onclick="s('9A65')">POWER</div>
 <div class="b x"></div>
 <div class="b mt" onclick="s('9867')">MUTE</div>
</div>
<div class="sep"></div>
<div class="g3">
 <div class="b" onclick="s('B24D')">EPG</div>
 <div class="b" onclick="s('708F')">INFO</div>
 <div class="b" onclick="s('B04F')">TTX</div>

 <div class="b" onclick="s('8A75')">AUDIO</div>
 <div class="b" onclick="s('48B7')">PVR</div>
 <div class="b" onclick="s('8877')">SUB</div>

 <div class="b" onclick="s('A25D')">MENU</div>
 <div class="b dp" onclick="s('609F')">&#9650;</div>
 <div class="b" onclick="s('A05F')">EXIT</div>

 <div class="b dp" onclick="s('5AA5')">&#9664;<br><small>V-</small></div>
 <div class="b ok" onclick="s('58A7')">OK</div>
 <div class="b dp" onclick="s('D827')">&#9654;<br><small>V+</small></div>

 <div class="b" onclick="s('AA55')">FAV</div>
 <div class="b dp" onclick="s('6897')">&#9660;</div>
 <div class="b" onclick="s('A857')">TV</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b n" onclick="s('4AB5')">1</div>
 <div class="b n" onclick="s('0AF5')">2</div>
 <div class="b n" onclick="s('08F7')">3</div>
 <div class="b" onclick="s('C837')">BACK</div>

 <div class="b n" onclick="s('6A95')">4</div>
 <div class="b n" onclick="s('2AD5')">5</div>
 <div class="b n" onclick="s('28D7')">6</div>
 <div class="b" onclick="s('E817')">SEEK</div>

 <div class="b n" onclick="s('728D')">7</div>
 <div class="b n" onclick="s('32CD')">8</div>
 <div class="b n" onclick="s('30CF')">9</div>
 <div class="b n" onclick="s('F00F')">0</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b" onclick="s('52AD')">&lt;&lt;REW</div>
 <div class="b" onclick="s('12ED')">FF&gt;&gt;</div>
 <div class="b" onclick="s('10EF')">|&#9664;</div>
 <div class="b" onclick="s('D02F')">&#9654;|</div>

 <div class="b" onclick="s('629D')">&#9654;PLAY</div>
 <div class="b" onclick="s('22DD')">&#10074;&#10074;</div>
 <div class="b" onclick="s('20DF')">&#9632;STOP</div>
 <div class="b" onclick="s('E01F')">&#8634;RPT</div>
</div>
<div class="sep"></div>
<div class="g4">
 <div class="b rd" onclick="s('42BD')">RED</div>
 <div class="b gn" onclick="s('02FD')">GREEN</div>
 <div class="b yl" onclick="s('00FF')">YELLOW</div>
 <div class="b bl" onclick="s('C03F')">BLUE</div>
</div>
<div id="L"></div>
<div class="ti">DMR-39 IR Remote</div>
</div>
<script>
function s(c){
 document.getElementById('L').textContent='...';
 fetch('/ir?code='+c).then(r=>r.text()).then(t=>document.getElementById('L').textContent=t).catch(e=>document.getElementById('L').textContent='ERR');
}
</script>
</body></html>)rawliteral";

// === Web handlers ===
static void handleRoot() { server.send(200, "text/html", PAGE); }

static bool isHex(const String& s, int maxLen) {
    if (s.length() == 0 || (int)s.length() > maxLen) return false;
    for (unsigned i = 0; i < s.length(); i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

static void handleIR() {
    if (!server.hasArg("code")) { server.send(400, "text/plain", "?code="); return; }
    String codeArg = server.arg("code");
    if (!isHex(codeArg, 8)) { server.send(400, "text/plain", "bad code"); return; }

    String hex = "0xFF" + codeArg;
    uint32_t code = strtoul(hex.c_str(), nullptr, 16);
    Serial.printf("[WEB] NEC 0x%06lX\n", (unsigned long)code);
    sendNEC(code);
    char resp[32];
    snprintf(resp, sizeof(resp), "OK 0x%06lX", (unsigned long)code);
    server.send(200, "text/plain", resp);
}

// === Serial ===
static char cmdBuf[64];
static int cmdLen = 0;

static void processCommand() {
    cmdBuf[cmdLen] = 0;
    while (cmdLen > 0 && cmdBuf[cmdLen-1] <= ' ') cmdBuf[--cmdLen] = 0;
    if (cmdLen == 0) return;
    const char* cmd = cmdBuf;

    if (strcmp(cmd, "fwd") == 0) {
        autoForward = !autoForward;
        Serial.printf("Auto-forward: %s\n", autoForward ? "ON" : "OFF");
    } else if (strcmp(cmd, "st") == 0) {
        Serial.printf("RX=%d TX=%d VCC=%d WiFi=%s IP=%s Caps=%d\n",
            digitalRead(IR_RX_PIN), digitalRead(IR_TX_PIN), digitalRead(STB_VCC),
            WiFi.isConnected() ? "OK" : "NO", WiFi.localIP().toString().c_str(), captureNum);
    } else if (strcmp(cmd, "?") == 0) {
        Serial.println(F("  fwd st ? <hex>=sendNEC"));
    } else {
        uint32_t code = strtoul(cmd, nullptr, 16);
        if (code > 0) { Serial.printf("NEC 0x%lX\n", (unsigned long)code); sendNEC(code); }
        else Serial.printf("? '%s'\n", cmd);
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println(F("\n=== DMR-39 IR Web Remote ==="));

    pinMode(IR_TX_PIN, OUTPUT);
    digitalWrite(IR_TX_PIN, HIGH);
    pinMode(STB_VCC, INPUT);

    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("WiFi: %s", WIFI_SSID);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
    Serial.println(WiFi.isConnected() ? String("\nIP: ") + WiFi.localIP().toString() : "\nWiFi FAIL");

    // OTA with password
    ArduinoOTA.setHostname("dmr39-ir");
    ArduinoOTA.setPassword(OTA_PASSWORD);
    ArduinoOTA.onStart([]() {
        irrecv.disableIRIn();
        Serial.println("[OTA] Start");
    });
    ArduinoOTA.onEnd([]()   { Serial.println("\n[OTA] Done"); });
    ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[OTA] Error %u\n", e); });
    ArduinoOTA.onProgress([](unsigned int prog, unsigned int total) {
        Serial.printf("[OTA] %u%%\r", prog * 100 / total);
    });
    ArduinoOTA.begin();

    server.on("/", handleRoot);
    server.on("/ir", handleIR);
    server.begin();

    irrecv.setUnknownThreshold(12);
    irrecv.enableIRIn();
    Serial.println(F("Ready. OTA enabled. Type ? for help.\n"));
}

void loop() {
    ArduinoOTA.handle();
    server.handleClient();

    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') { if (cmdLen > 0) { processCommand(); cmdLen = 0; } }
        else if (cmdLen < 63) cmdBuf[cmdLen++] = c;
    }

    if (irrecv.decode(&results)) {
        captureNum++;
        uint16_t* raw = resultToRawArray(&results);
        if (raw == nullptr) { irrecv.resume(); return; }
        lastRawLen = getCorrectedRawLength(&results);
        if (lastRawLen > 256) lastRawLen = 256;
        memcpy(lastRaw, raw, lastRawLen * sizeof(uint16_t));
        delete[] raw;
        lastProto = results.decode_type;
        lastValue = results.value;

        Serial.printf("[CAP #%d] %s", captureNum, typeToString(lastProto).c_str());
        if (lastProto != decode_type_t::UNKNOWN) Serial.printf(" 0x%llX", lastValue);
        if (autoForward) { Serial.print(" ->FWD"); replayRaw(lastRaw, lastRawLen); }
        Serial.println();
        irrecv.resume();
    }

    static uint32_t lastAlive = 0;
    if (millis() - lastAlive > 30000) {
        lastAlive = millis();
        if (!WiFi.isConnected()) {
            Serial.println("[WiFi] Reconnecting...");
            WiFi.reconnect();
        }
        Serial.printf("[%lu] alive vcc=%d caps=%d ip=%s\n",
            millis(), digitalRead(STB_VCC), captureNum,
            WiFi.isConnected() ? WiFi.localIP().toString().c_str() : "none");
    }
}
