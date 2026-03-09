#include <Arduino.h>
#include <Wire.h>
#include <ESP8266WiFi.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>

extern "C" {
#include <ets_sys.h>
#include <gpio.h>
}

#include "config.h"
#include "hw650_emu.h"
#include "analyzer.h"

// ── Solder-blob bridge mode ──
// STB and front panel share one I2C bus (blobs on D1↔D2=SDA, D3↔D4=SCL).
// ESP passively sniffs. No Wire master (would conflict with STB master).
// Front panel HW650EP responds to STB directly.

// ── Button pins (parallel taps from front panel buttons) ──
#define BTN_POWER  14  // GPIO14 = D5
#define BTN_A      12  // GPIO12 = D6 (CH+ or CH-)
#define BTN_B      13  // GPIO13 = D7 (CH+ or CH-)

// ── Front panel key scan state ──
bool fpEnabled = false;
uint32_t lastFpPoll = 0;
uint32_t lastBtnPrint = 0;
uint8_t lastKeyCode = 0;
uint8_t lastBtnPower = 1, lastBtnA = 1, lastBtnB = 1;

AsyncWebServer server(HTTP_PORT);
AsyncWebSocket ws("/ws");
HW650Emulator emu;
SignalAnalyzer analyzer;

OperatingMode currentMode = MODE_ANALYZER;
uint32_t lastBroadcast = 0;
bool wifiUp = false;
bool httpUp = false;

void setupWiFi();
void setupHTTP();
void onWsEvent(AsyncWebSocket *srv, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len);
void broadcastDisplay();
void handleSerialCommand();
void runNormalMode();

// Key codes discovered from front panel HW650EP:
// Drive D7(GPIO13) HIGH → 0x44, Drive D6(GPIO12) HIGH → 0x4C, Drive D5(GPIO14) LOW → 0x34
// Which is CH+ vs CH- TBD — need to test with STB
#define KEY_STANDBY  0x34   // Power button (D5 LOW)
#define KEY_CH_A     0x4C   // Button A = D6 HIGH (CH+ or CH-)
#define KEY_CH_B     0x44   // Button B = D7 HIGH (CH+ or CH-)
// Aliases until we know which is which
#define KEY_CH_UP    KEY_CH_A
#define KEY_CH_DOWN  KEY_CH_B

// Key responder key code — shared between inject logic and I2C responder
static volatile uint8_t kr_keyCode = 0x74;  // 0x74 = no key pressed

// ── Non-blocking key injection via GPIO ──
// Drives the front panel button pin for a duration to simulate a press.
// The HW650EP detects it and reports the key code on its I2C bus.
static struct {
    uint8_t pin;
    uint8_t level;
    uint32_t endTime;
    bool active;
} keyInject = {0, LOW, 0, false};

// Ensure all button pins float (INPUT, no pullup) except the one being driven
void floatAllButtons() {
    pinMode(BTN_POWER, INPUT);
    pinMode(BTN_A, INPUT);
    pinMode(BTN_B, INPUT);
}

void startKeyInject(uint8_t keyCode, uint16_t duration_ms = 200) {
    if (keyInject.active) return;  // already injecting
    switch (keyCode) {
        case KEY_CH_A:    keyInject.pin = BTN_A;     keyInject.level = HIGH; break;
        case KEY_CH_B:    keyInject.pin = BTN_B;     keyInject.level = HIGH; break;
        case KEY_STANDBY: keyInject.pin = BTN_POWER; keyInject.level = LOW;  break;
        default: return;
    }
    // Float ALL button pins first to avoid loading the matrix
    floatAllButtons();
    // Then drive only the target pin
    pinMode(keyInject.pin, OUTPUT);
    digitalWrite(keyInject.pin, keyInject.level);
    // Also set the key code for STB bus I2C responder
    kr_keyCode = keyCode;
    keyInject.endTime = millis() + duration_ms;
    keyInject.active = true;
    Serial.printf("KEY INJECT: pin=%d level=%d code=0x%02X for %ums\n",
        keyInject.pin, keyInject.level, keyCode, duration_ms);
}

void pollKeyInject() {
    if (!keyInject.active) return;
    if (millis() >= keyInject.endTime) {
        pinMode(keyInject.pin, INPUT);  // float — no pullup to avoid loading button matrix
        kr_keyCode = 0x74;  // back to "no key" for STB bus
        keyInject.active = false;
        Serial.println(F("KEY INJECT: released"));
    }
}

// ── I2C Key Responder V3 — Direct hardware ISR, WiFi-off ──
// Improvements over V2:
//  1. WiFi radio OFF (no background WiFi interrupts stealing CPU)
//  2. Direct ETS GPIO ISR registration (skip Arduino dispatch table ~1-2μs saved)
//  3. Single GPI read per interrupt (consistent pin state snapshot)
//  4. STOP detection (SDA rise while SCL high → reset state machine)
//  5. Bit timeout (200μs between CLK edges → stale transaction)
//  6. -O2 compiler optimization (vs default -Os)

enum KR_State : uint8_t {
    KR_IDLE = 0,
    KR_ADDR_BITS,       // reading 8 address bits (sample on CLK rise)
    KR_ADDR_ACK_SETUP,  // CLK fell after bit 7 — drive ACK
    KR_ADDR_ACK_HOLD,   // ACK driven, wait for CLK cycle
    KR_WRITE_BITS,      // receiving data byte (sample on CLK rise)
    KR_WRITE_ACK_SETUP, // drive ACK for received data
    KR_WRITE_ACK_HOLD,  // ACK driven for data
    KR_READ_BIT_SETUP,  // driving data bit (set on CLK fall)
    KR_READ_MACK,       // master ACK/NACK after our byte
    KR_DONE
};

static volatile KR_State kr_state = KR_IDLE;
static volatile uint8_t kr_byte = 0;
static volatile uint8_t kr_bitPos = 0;
static volatile uint8_t kr_addrByte = 0;
static volatile uint8_t kr_sendData = 0;
static volatile bool kr_isRead = false;
static volatile uint32_t kr_lastEdgeUs = 0;  // V3: for bit timeout

// Stats
static volatile uint32_t kr_startCount = 0;
static volatile uint32_t kr_stopCount = 0;    // V3: STOP detection
static volatile uint32_t kr_noiseCount = 0;
static volatile uint32_t kr_handleCount = 0;
static volatile uint32_t kr_nackCount = 0;
static volatile uint32_t kr_timeoutCount = 0; // V3: bit timeouts
static volatile uint8_t kr_lastAddr = 0;
static volatile uint8_t kr_addrLog[16];
static volatile uint8_t kr_addrLogIdx = 0;
// Write transaction log (for display data forwarding)
struct KR_WriteTx { uint8_t addr; uint8_t data; };
static volatile KR_WriteTx kr_writeLog[32];
static volatile uint8_t kr_writeLogIdx = 0;
static volatile uint32_t kr_writeCount = 0;

// Direct GPIO register access (IRAM-safe)
#define KR_SDA_BIT  (1 << PIN_DAT)
#define KR_SCL_BIT  (1 << PIN_CLK)
#define KR_SDA_LOW()    do { GPES = KR_SDA_BIT; GPOC = KR_SDA_BIT; } while(0)
#define KR_SDA_HIGH()   do { GPEC = KR_SDA_BIT; } while(0)

// V3: Timeout for stale transactions (200μs = 10x the 20μs bit period at 50kHz)
#define KR_BIT_TIMEOUT_US 200

// Debug mode: ACK all addresses to discover what the STB sends
#define KR_ACK_ALL 1

static bool IRAM_ATTR kr_isOurAddr(uint8_t addr7) {
#if KR_ACK_ALL
    // ACK everything for bus discovery
    return (addr7 > 0 && addr7 < 0x78);  // valid 7-bit I2C range
#else
    return (addr7 >= 0x24 && addr7 <= 0x27) ||
           (addr7 >= 0x34 && addr7 <= 0x37);
#endif
}

// V3: Single unified GPIO ISR — handles both SDA and SCL edges
// Registered directly via ETS_GPIO_INTR_ATTACH, bypassing Arduino's dispatch table.
// This saves ~1-2μs per interrupt vs attachInterrupt().
void IRAM_ATTR kr_gpio_isr(void *arg) {
    uint32_t status = GPIE;   // which pins triggered (interrupt status register)
    GPIEC = status;           // clear those interrupt flags immediately
    uint32_t pins = GPI;      // snapshot ALL GPIO pin states at once

    // Extract pin states from single read (consistent snapshot)
    uint8_t sda = (pins >> PIN_DAT) & 1;
    uint8_t scl = (pins >> PIN_CLK) & 1;
    bool sda_changed = status & KR_SDA_BIT;
    bool scl_changed = status & KR_SCL_BIT;

    // ── Priority 1: START / STOP detection (SDA edge while SCL high) ──
    if (sda_changed && scl) {
        if (!sda) {
            // SDA fell while SCL high = START condition
            kr_startCount++;
            kr_state = KR_ADDR_BITS;
            kr_byte = 0;
            kr_bitPos = 0;
            kr_lastEdgeUs = micros();
            return;
        } else {
            // SDA rose while SCL high = STOP condition
            if (kr_state != KR_IDLE) {
                KR_SDA_HIGH();
                kr_state = KR_IDLE;
            }
            kr_stopCount++;
            return;
        }
    }

    // ── Priority 2: SCL edge → bit operations ──
    if (!scl_changed || kr_state == KR_IDLE) return;

    // V3: Bit timeout — if too long since last edge, transaction is stale
    uint32_t now = micros();
    if (now - kr_lastEdgeUs > KR_BIT_TIMEOUT_US) {
        KR_SDA_HIGH();
        kr_state = KR_IDLE;
        kr_timeoutCount++;
        return;
    }
    kr_lastEdgeUs = now;

    if (scl) {
        // ── SCL ROSE → sample SDA (using pre-read snapshot) ──
        switch (kr_state) {
            case KR_ADDR_BITS:
                kr_byte = (kr_byte << 1) | sda;
                kr_bitPos++;
                if (kr_bitPos >= 8) kr_state = KR_ADDR_ACK_SETUP;
                break;
            case KR_ADDR_ACK_HOLD:
                break;  // master reads our ACK
            case KR_WRITE_BITS:
                kr_byte = (kr_byte << 1) | sda;
                kr_bitPos++;
                if (kr_bitPos >= 8) kr_state = KR_WRITE_ACK_SETUP;
                break;
            case KR_WRITE_ACK_HOLD:
                break;  // master reads our data ACK
            case KR_READ_BIT_SETUP:
                kr_bitPos++;  // master sampled our bit
                if (kr_bitPos >= 8) kr_state = KR_READ_MACK;
                break;
            case KR_READ_MACK:
                break;  // master ACK/NACK
            default: break;
        }
    } else {
        // ── SCL FELL → drive SDA ──
        switch (kr_state) {
            case KR_ADDR_ACK_SETUP: {
                kr_addrByte = kr_byte;
                kr_lastAddr = kr_byte;
                if (kr_addrLogIdx < 16) kr_addrLog[kr_addrLogIdx++] = kr_byte;
                uint8_t addr7 = kr_byte >> 1;
                kr_isRead = kr_byte & 1;
                if (kr_isOurAddr(addr7)) {
                    KR_SDA_LOW();  // ACK!
                    kr_state = KR_ADDR_ACK_HOLD;
                    kr_handleCount++;
                } else {
                    kr_nackCount++;
                    kr_state = KR_IDLE;
                }
                break;
            }
            case KR_ADDR_ACK_HOLD:
                KR_SDA_HIGH();  // release after ACK
                if (kr_isRead) {
                    kr_sendData = kr_keyCode;
                    kr_bitPos = 0;
                    if (kr_sendData & 0x80) KR_SDA_HIGH(); else KR_SDA_LOW();
                    kr_state = KR_READ_BIT_SETUP;
                } else {
                    kr_byte = 0;
                    kr_bitPos = 0;
                    kr_state = KR_WRITE_BITS;
                }
                break;
            case KR_WRITE_ACK_SETUP:
                KR_SDA_LOW();  // ACK data byte
                kr_state = KR_WRITE_ACK_HOLD;
                break;
            case KR_WRITE_ACK_HOLD:
                KR_SDA_HIGH();
                kr_state = KR_IDLE;
                break;
            case KR_READ_BIT_SETUP:
                if (kr_bitPos < 8) {
                    if (kr_sendData & (0x80 >> kr_bitPos)) KR_SDA_HIGH(); else KR_SDA_LOW();
                } else {
                    KR_SDA_HIGH();  // release for master ACK
                }
                break;
            case KR_READ_MACK:
                KR_SDA_HIGH();
                kr_state = KR_IDLE;
                break;
            default: break;
        }
    }

    if (kr_state == KR_DONE) {
        KR_SDA_HIGH();
        kr_state = KR_IDLE;
    }
}

void setupKeyResponder() {
    pinMode(PIN_DAT, INPUT_PULLUP);
    pinMode(PIN_CLK, INPUT_PULLUP);
    kr_state = KR_IDLE;

    // V3: Direct ETS GPIO interrupt registration — bypass Arduino dispatch table
    ETS_GPIO_INTR_DISABLE();

    // Configure SDA (PIN_DAT) for ANYEDGE (START + STOP detection)
    GPC(PIN_DAT) = (GPC(PIN_DAT) & ~(0xF << GPCI)) | (GPIO_PIN_INTR_ANYEDGE << GPCI);
    // Configure SCL (PIN_CLK) for ANYEDGE (rise = sample, fall = drive)
    GPC(PIN_CLK) = (GPC(PIN_CLK) & ~(0xF << GPCI)) | (GPIO_PIN_INTR_ANYEDGE << GPCI);

    // Clear pending interrupts
    GPIEC = KR_SDA_BIT | KR_SCL_BIT;

    // Register our handler directly on the GPIO interrupt vector
    ETS_GPIO_INTR_ATTACH(kr_gpio_isr, NULL);
    ETS_GPIO_INTR_ENABLE();

    Serial.println(F("I2C Key Responder V3 (direct ISR, WiFi-off, -O2) on D2/D1"));
}

void detachKeyResponder() {
    // Disable interrupts for our pins
    GPC(PIN_DAT) = (GPC(PIN_DAT) & ~(0xF << GPCI));  // disable SDA interrupt
    GPC(PIN_CLK) = (GPC(PIN_CLK) & ~(0xF << GPCI));  // disable SCL interrupt
    KR_SDA_HIGH();
    kr_state = KR_IDLE;
}

// ── I2C Key Responder V4 — Tight polling loop, unrolled state machine ──
// No interrupts at all. Pure polling of GPI register.
// At 160MHz, each GPI read + edge check is ~30ns → ~33MHz effective poll rate.
// This gives ~133 chances to detect a START within its ~4μs hold window.
// Trade-off: blocks main loop during polling burst, but STB only polls at ~5Hz.

static volatile bool kr_v4_active = false;

// Handle a complete I2C transaction after START detected.
// Unrolled: each phase is a separate while-loop waiting for the specific edge.
// No switch/case, no state machine dispatch overhead.
void IRAM_ATTR kr_handleTransactionV4() {
    uint8_t byte = 0;
    uint32_t deadline = micros() + 500;  // 500μs absolute timeout

    // CRITICAL: After START, SCL is still HIGH. We must wait for SCL LOW
    // (end of START hold) before reading address bits, otherwise we sample
    // the START SDA state as a phantom bit and get a 1-bit shift.
    while (GPI & KR_SCL_BIT) {
        if (micros() > deadline) { kr_timeoutCount++; return; }
    }

    // ── Phase 1: Read 8 address bits (sample SDA on SCL rising edge) ──
    for (uint8_t bit = 0; bit < 8; bit++) {
        // Wait for SCL rise
        while (!(GPI & KR_SCL_BIT)) {
            if (micros() > deadline) { kr_timeoutCount++; return; }
        }
        // Sample SDA
        byte = (byte << 1) | ((GPI >> PIN_DAT) & 1);
        // Wait for SCL fall
        while (GPI & KR_SCL_BIT) {
            if (micros() > deadline) { kr_timeoutCount++; return; }
        }
    }

    // Got full address byte
    kr_startCount++;
    kr_addrByte = byte;
    kr_lastAddr = byte;
    if (kr_addrLogIdx < 16) kr_addrLog[kr_addrLogIdx++] = byte;

    uint8_t addr7 = byte >> 1;
    bool isRead = byte & 1;

    if (!kr_isOurAddr(addr7)) {
        kr_nackCount++;
        return;  // Not our address — don't ACK, let bus float
    }

    // ── Phase 2: ACK the address ──
    KR_SDA_LOW();  // Drive ACK
    kr_handleCount++;
    // Wait for SCL rise (master samples our ACK)
    while (!(GPI & KR_SCL_BIT)) {
        if (micros() > deadline) { KR_SDA_HIGH(); return; }
    }
    // Wait for SCL fall (ACK cycle done)
    while (GPI & KR_SCL_BIT) {
        if (micros() > deadline) { KR_SDA_HIGH(); return; }
    }
    KR_SDA_HIGH();  // Release SDA

    if (isRead) {
        // ── Phase 3a: Send data byte (master reads from us) ──
        uint8_t data = kr_keyCode;
        for (uint8_t bit = 0; bit < 8; bit++) {
            // Drive data bit on SCL low
            if (data & (0x80 >> bit)) KR_SDA_HIGH(); else KR_SDA_LOW();
            // Wait for SCL rise (master samples our bit)
            while (!(GPI & KR_SCL_BIT)) {
                if (micros() > deadline) { KR_SDA_HIGH(); return; }
            }
            // Wait for SCL fall
            while (GPI & KR_SCL_BIT) {
                if (micros() > deadline) { KR_SDA_HIGH(); return; }
            }
        }
        KR_SDA_HIGH();  // Release for master ACK/NACK
        // Wait for master's ACK/NACK clock
        while (!(GPI & KR_SCL_BIT)) {
            if (micros() > deadline) return;
        }
        while (GPI & KR_SCL_BIT) {
            if (micros() > deadline) return;
        }
    } else {
        // ── Phase 3b: Receive data byte (master writes to us) ──
        byte = 0;
        for (uint8_t bit = 0; bit < 8; bit++) {
            // Wait for SCL rise
            while (!(GPI & KR_SCL_BIT)) {
                if (micros() > deadline) return;
            }
            byte = (byte << 1) | ((GPI >> PIN_DAT) & 1);
            // Wait for SCL fall
            while (GPI & KR_SCL_BIT) {
                if (micros() > deadline) return;
            }
        }
        // ACK the data byte
        KR_SDA_LOW();
        while (!(GPI & KR_SCL_BIT)) {
            if (micros() > deadline) { KR_SDA_HIGH(); return; }
        }
        while (GPI & KR_SCL_BIT) {
            if (micros() > deadline) { KR_SDA_HIGH(); return; }
        }
        KR_SDA_HIGH();
        // Log received write: addr + data for display forwarding
        if (kr_writeLogIdx < 32) {
            kr_writeLog[kr_writeLogIdx].addr = kr_addrByte;
            kr_writeLog[kr_writeLogIdx].data = byte;
            kr_writeLogIdx++;
        }
        kr_writeCount++;
    }
}

// V4 polling burst: poll GPI for START conditions.
// Returns quickly if a transaction was handled or after max_polls iterations.
void IRAM_ATTR kr_pollBurst(uint32_t max_polls) {
    uint32_t prev = GPI;
    for (uint32_t i = 0; i < max_polls; i++) {
        uint32_t curr = GPI;
        // Detect START: SDA was HIGH, now LOW, while SCL is HIGH
        if ((prev & KR_SDA_BIT) && !(curr & KR_SDA_BIT) && (curr & KR_SCL_BIT)) {
            kr_handleTransactionV4();
            return;  // Handled one transaction, return to let main loop do housekeeping
        }
        prev = curr;
    }
}

const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DMR-39 Bridge</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #eee;
         display: flex; flex-direction: column; align-items: center; padding: 1em; }
  h1 { font-size: 1.2em; margin-bottom: 0.5em; color: #888; }
  .display { font-family: 'Courier New', monospace; font-size: 4em; color: #0f0;
             background: #111; padding: 0.2em 0.5em; border-radius: 8px;
             letter-spacing: 0.2em; text-align: center; margin: 0.5em 0;
             border: 1px solid #333; min-width: 5em; }
  .status { font-size: 0.8em; margin-bottom: 1em; }
  .status.ok { color: #4a4; }
  .status.err { color: #a44; }
  .buttons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
             max-width: 300px; width: 100%; margin: 0.5em 0; }
  button { padding: 1em; font-size: 1.1em; border: none; border-radius: 8px;
           cursor: pointer; background: #2a2a4a; color: #eee;
           -webkit-tap-highlight-color: transparent; }
  button:active { background: #4a4a7a; }
  button.power { background: #622; }
  button.power:active { background: #944; }
  .stats { font-size: 0.7em; color: #555; margin-top: 0.5em; }
  .log { width: 100%; max-width: 400px; margin-top: 1em; font-size: 0.75em;
         background: #111; padding: 0.5em; border-radius: 4px; max-height: 200px;
         overflow-y: auto; font-family: monospace; color: #888; white-space: pre-wrap; }
</style>
</head>
<body>
<h1>DMR-39 STB Bridge</h1>
<div class="status" id="status">Connecting...</div>
<div class="display" id="display">----</div>
<div class="buttons">
  <div></div>
  <button onclick="cmd('key_inject','ch_up')">CH +</button>
  <div></div>
  <button class="power" onclick="cmd('key_inject','standby')">POWER</button>
  <button onclick="cmd('read_display')">Refresh</button>
  <button onclick="cmd('get_status')">Status</button>
  <div></div>
  <button onclick="cmd('key_inject','ch_down')">CH -</button>
  <div></div>
</div>
<div class="stats" id="stats"></div>
<div class="log" id="log"></div>
<script>
let ws;
const $ = id => document.getElementById(id);
function connect() {
  ws = new WebSocket('ws://' + location.host + '/ws');
  ws.onopen = () => { $('status').textContent = 'Connected'; $('status').className = 'status ok'; };
  ws.onclose = () => { $('status').textContent = 'Disconnected'; $('status').className = 'status err';
                        setTimeout(connect, 2000); };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === 'display') {
      $('display').textContent = m.text || '----';
      log('Display: ' + m.text + ' [' + (m.segments||[]).map(x=>'0x'+x.toString(16).padStart(2,'0')).join(' ') + ']');
    } else if (m.type === 'status') {
      $('stats').textContent = 'TX: ' + (m.tx_count||0) + ' | Errors: ' + (m.err_count||0) + ' | Mode: ' + (m.mode||'?');
      log('Mode: ' + m.mode + ', Key: 0x' + (m.key_response||0).toString(16).padStart(2,'0'));
    } else if (m.type === 'info') {
      log(m.msg);
    } else if (m.type === 'analyzer') {
      log('Analyzer: ' + m.events + ' events, CLK=' + m.clk_freq_hz + ' Hz');
    }
  };
}
function cmd(c, arg) {
  if (ws && ws.readyState === 1) {
    const msg = {cmd: c};
    if (arg) msg.arg = arg;
    ws.send(JSON.stringify(msg));
  }
}
function log(msg) {
  const el = $('log');
  const t = new Date().toLocaleTimeString();
  el.appendChild(document.createTextNode(t + ' ' + msg + '\n'));
  while (el.childNodes.length > 200) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}
connect();
</script>
</body>
</html>
)rawliteral";

// ── Front Panel I2C Master functions ──
// Write a byte to the front panel's HW650EP via I2C
// addr8 is the 8-bit address (e.g., 0x48, 0x68, 0x6A...)
bool fpWrite(uint8_t addr8, uint8_t data) {
    Wire.beginTransmission(addr8 >> 1);  // convert to 7-bit
    Wire.write(data);
    return Wire.endTransmission() == 0;
}

// Read a byte from the front panel's HW650EP
uint8_t fpRead(uint8_t addr8) {
    Wire.requestFrom((uint8_t)(addr8 >> 1), (uint8_t)1);
    if (Wire.available()) return Wire.read();
    return 0;
}

// setupFrontPanel() — DISABLED in solder-blob mode.
// STB talks to front panel directly via blob bridge.
// ESP does NOT use Wire master (would conflict with STB master on shared bus).

// pollFrontPanel() — DISABLED in solder-blob mode.
// STB handles all front panel I2C communication directly.

void setup() {
    Serial.begin(230400);
    Serial.println();
    Serial.println(F("=== DMR-39 STB Bridge (Solder-Blob Mode) ==="));
    Serial.println(F("Shared bus: SDA=D1+D2(blob) SCL=D3+D4(blob) VDD=3.3V"));
    Serial.println();

    // Pin-level diagnostic: SDA=GPIO4(D2), SCL=GPIO0(D3) — shared bus via solder blobs
    Serial.println(F("=== PIN DIAGNOSTIC (shared bus) ==="));
    pinMode(PIN_DAT, INPUT_PULLUP);
    pinMode(PIN_CLK, INPUT_PULLUP);

    // Count edges on SDA and SCL for 1 second
    uint32_t edgesSDA = 0, edgesSCL = 0;
    uint8_t prevSDA = digitalRead(PIN_DAT), prevSCL = digitalRead(PIN_CLK);
    uint32_t start = millis();
    while (millis() - start < 1000) {
        uint8_t curSDA = digitalRead(PIN_DAT);
        uint8_t curSCL = digitalRead(PIN_CLK);
        if (curSDA != prevSDA) { edgesSDA++; prevSDA = curSDA; }
        if (curSCL != prevSCL) { edgesSCL++; prevSCL = curSCL; }
        if ((millis() - start) % 200 == 0) ESP.wdtFeed();
    }
    Serial.printf("SDA GPIO%d (D2): %lu edges/sec (idle=%d)\n", PIN_DAT, (unsigned long)edgesSDA, digitalRead(PIN_DAT));
    Serial.printf("SCL GPIO%d (D3): %lu edges/sec (idle=%d)\n", PIN_CLK, (unsigned long)edgesSCL, digitalRead(PIN_CLK));
    if (edgesSDA == 0 && edgesSCL == 0) {
        Serial.println(F(">>> No activity — check solder blobs and STB power"));
    } else {
        Serial.println(F(">>> Bus active — solder blob bridge working"));
    }

    // Capture a micro-burst with timestamps
    Serial.println(F("\n=== MICRO-BURST CAPTURE (500ms) ==="));
    struct { uint32_t us; uint8_t sda; uint8_t scl; } burst[256];
    uint16_t bcount = 0;
    prevSDA = digitalRead(PIN_DAT);
    prevSCL = digitalRead(PIN_CLK);
    uint32_t startUs = micros();
    uint32_t endUs = startUs + 500000;
    uint32_t lastFeed = millis();
    while (micros() < endUs && bcount < 256) {
        uint8_t curSDA = digitalRead(PIN_DAT);
        uint8_t curSCL = digitalRead(PIN_CLK);
        if (curSDA != prevSDA || curSCL != prevSCL) {
            burst[bcount].us = micros() - startUs;
            burst[bcount].sda = curSDA;
            burst[bcount].scl = curSCL;
            bcount++;
            prevSDA = curSDA;
            prevSCL = curSCL;
        }
        if (millis() - lastFeed > 200) { ESP.wdtFeed(); lastFeed = millis(); }
    }
    Serial.printf("Captured %u events\n", bcount);
    Serial.println(F("  Time(us)  SDA(D2)  SCL(D3)"));
    for (uint16_t i = 0; i < bcount && i < 100; i++) {
        Serial.printf("  %8lu      %d        %d\n",
            (unsigned long)burst[i].us, burst[i].sda, burst[i].scl);
    }
    Serial.println(F("=== END DIAGNOSTIC ===\n"));

    // Start passive bus analyzer (sniff-only, no driving)
    analyzer.begin(PIN_DAT, PIN_CLK);
    emu.begin(PIN_DAT, PIN_CLK);
    emu.setAutoClear(true);
    currentMode = MODE_NORMAL;

    // Solder-blob bridge: STB talks to front panel directly.
    // No key responder needed (real HW650EP responds).
    // No Wire master needed (would conflict with STB master on shared bus).
    // setupKeyResponder();  // DISABLED — real front panel handles key responses
    // setupFrontPanel();    // DISABLED — ESP is passive on shared bus

    // Setup button GPIO pins only (for remote key injection via D5/D6/D7)
    Serial.println(F("\n=== GPIO BUTTON SETUP ==="));
    pinMode(BTN_POWER, INPUT);  // float — no pullup to avoid loading button matrix
    pinMode(BTN_A, INPUT);
    pinMode(BTN_B, INPUT);
    Serial.println(F("Button pins: D5=Power D6=BtnA D7=BtnB (float, no pullup)"));

    // WiFi: disabled for maximum ISR performance, or connect if enabled
#ifdef WIFI_DISABLED
    WiFi.mode(WIFI_OFF);
    WiFi.forceSleepBegin();
    delay(1);  // let modem actually power down
    Serial.println(F("WiFi OFF — max ISR performance, serial-only control"));
#else
    setupWiFi();
    if (wifiUp) setupHTTP();
#endif

    Serial.println(F("Serial: f=freq d=decode a=edges s=sniff p=pin-test i/j=inject x=stats h=help"));
}

void loop() {
    handleSerialCommand();
    pollKeyInject();  // non-blocking GPIO key injection state machine

    // Solder-blob mode: passive sniff only, no I2C driving
    switch (currentMode) {
        case MODE_NORMAL: break;  // idle — GPIO inject handled by pollKeyInject()
        case MODE_ANALYZER: break;
        case MODE_PASSTHROUGH: analyzer.sniffAndPrint(Serial); break;
    }
    yield();
}

void runNormalMode() {
    // Solder-blob mode: no front panel polling (STB handles it directly).
    // Periodic WebSocket maintenance (only when WiFi is up)
#ifndef WIFI_DISABLED
    if (httpUp && millis() - lastBroadcast >= DISPLAY_POLL_MS) {
        lastBroadcast = millis();
        ws.cleanupClients();
    }
#endif
}

void broadcastDisplay() {
    char text[5];
    emu.getDisplayText(text);
    JsonDocument doc;
    doc["type"] = "display";
    doc["text"] = text;
    JsonArray segs = doc["segments"].to<JsonArray>();
    for (uint8_t i = 0; i < 4; i++) segs.add(emu.getDigit(i));
    doc["control"] = emu.getControl();
    char buf[192];
    serializeJson(doc, buf);
    if (httpUp) ws.textAll(buf);
    Serial.printf("Display: %s [%02X %02X %02X %02X] ctrl=%02X\n",
                  text, emu.getDigit(0), emu.getDigit(1),
                  emu.getDigit(2), emu.getDigit(3), emu.getControl());
}

void wsInfo(AsyncWebSocketClient *client, const char* msg) {
    JsonDocument doc;
    doc["type"] = "info";
    doc["msg"] = msg;
    char buf[256];
    serializeJson(doc, buf);
    client->text(buf);
}

void wsStatus(AsyncWebSocketClient *client) {
    JsonDocument doc;
    doc["type"] = "status";
    doc["mode"] = (currentMode == MODE_NORMAL) ? "normal" : "analyzer";
    doc["key_response"] = emu.getKeyResponse();
    doc["tx_count"] = emu.getTxCount();
    doc["err_count"] = emu.getErrorCount();
    char buf[192];
    serializeJson(doc, buf);
    client->text(buf);
}

void onWsEvent(AsyncWebSocket *srv, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len) {
    if (type == WS_EVT_CONNECT) {
        Serial.printf("WS client #%u from %s\n",
                      client->id(), client->remoteIP().toString().c_str());
        wsStatus(client);
    }
    if (type == WS_EVT_DATA) {
        AwsFrameInfo *info = (AwsFrameInfo*)arg;
        if (info->opcode != WS_TEXT) return;
        JsonDocument doc;
        if (deserializeJson(doc, (char*)data, len) != DeserializationError::Ok) return;
        const char* cmd = doc["cmd"];
        if (!cmd) return;
        if (strcmp(cmd, "read_display") == 0) {
            broadcastDisplay();
        } else if (strcmp(cmd, "get_status") == 0) {
            wsStatus(client);
        } else if (strcmp(cmd, "key_inject") == 0) {
            const char* arg = doc["arg"] | "?";
            uint8_t keyCode = 0;
            if (strcmp(arg, "standby") == 0)       keyCode = KEY_STANDBY;
            else if (strcmp(arg, "ch_up") == 0)     keyCode = KEY_CH_UP;
            else if (strcmp(arg, "ch_down") == 0)   keyCode = KEY_CH_DOWN;
            if (keyCode != 0) {
                startKeyInject(keyCode);
                char msg[128];
                snprintf(msg, sizeof(msg), "Key inject: '%s' = 0x%02X via GPIO", arg, keyCode);
                wsInfo(client, msg);
            }
        } else if (strcmp(cmd, "clear_key") == 0) {
            emu.clearKeyResponse();
            wsInfo(client, "Key cleared");
        } else if (strcmp(cmd, "analyzer_capture") == 0) {
            detachKeyResponder();
            analyzer.begin(PIN_DAT, PIN_CLK);
            uint16_t n = analyzer.captureEdges(2000);
            uint32_t freq = analyzer.measureClockFreqHz(200);
            JsonDocument resp;
            resp["type"] = "analyzer";
            resp["events"] = n;
            resp["clk_freq_hz"] = freq;
            if (n > 0) { analyzer.printEdgeLog(Serial); analyzer.decodeI2C(Serial); }
            char buf[192];
            serializeJson(resp, buf);
            client->text(buf);
            if (currentMode == MODE_NORMAL) { emu.begin(PIN_DAT, PIN_CLK); setupKeyResponder(); }
        }
    }
}

void setupHTTP() {
    ws.onEvent(onWsEvent);
    server.addHandler(&ws);
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
        req->send_P(200, "text/html", INDEX_HTML);
    });
    server.on("/api/status", HTTP_GET, [](AsyncWebServerRequest *req) {
        JsonDocument doc;
        doc["mode"] = (currentMode == MODE_NORMAL) ? "normal" : "analyzer";
        doc["wifi_rssi"] = WiFi.RSSI();
        doc["uptime_s"] = millis() / 1000;
        char text[5]; emu.getDisplayText(text);
        doc["display_text"] = text;
        doc["tx_count"] = emu.getTxCount();
        doc["err_count"] = emu.getErrorCount();
        char buf[256];
        serializeJson(doc, buf);
        req->send(200, "application/json", buf);
    });
    server.on("/api/key/standby", HTTP_GET, [](AsyncWebServerRequest *req) {
        startKeyInject(KEY_STANDBY);
        req->send(200, "text/plain", "OK: standby (GPIO inject)");
    });
    server.on("/api/key/ch_up", HTTP_GET, [](AsyncWebServerRequest *req) {
        startKeyInject(KEY_CH_UP);
        req->send(200, "text/plain", "OK: ch_up (GPIO inject)");
    });
    server.on("/api/key/ch_down", HTTP_GET, [](AsyncWebServerRequest *req) {
        startKeyInject(KEY_CH_DOWN);
        req->send(200, "text/plain", "OK: ch_down (GPIO inject)");
    });
    DefaultHeaders::Instance().addHeader("Access-Control-Allow-Origin", "*");
    server.begin();
    httpUp = true;
    Serial.printf("Web server: http://%s/\n", WiFi.localIP().toString().c_str());
}

void wifiScan() {
    Serial.println(F("WiFi scan..."));
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    int n = WiFi.scanNetworks();
    Serial.printf("Found %d networks:\n", n);
    for (int i = 0; i < n; i++) {
        Serial.printf("  %2d: %-32s  %ddBm  ch%d  %s\n",
            i, WiFi.SSID(i).c_str(), WiFi.RSSI(i), WiFi.channel(i),
            WiFi.encryptionType(i) == ENC_TYPE_NONE ? "open" : "encrypted");
    }
}

void setupWiFi() {
    const char* ssid = WIFI_SSID;
    const char* pass = WIFI_PASS;
    if (strlen(ssid) == 0) {
        Serial.println(F("No SSID — AP mode: DMR39-Bridge"));
        WiFi.mode(WIFI_AP);
        WiFi.softAP("DMR39-Bridge", "dmr39esp8266");
        wifiUp = true;
        Serial.printf("AP IP: %s\n", WiFi.softAPIP().toString().c_str());
        return;
    }
    Serial.printf("WiFi: '%s'...", ssid);
    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid, pass);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
        delay(500);
        Serial.print('.');
    }
    if (WiFi.isConnected()) {
        wifiUp = true;
        Serial.printf("\nIP: %s (RSSI: %d dBm)\n",
            WiFi.localIP().toString().c_str(), WiFi.RSSI());
    } else {
        Serial.println(F("\nSTA failed → AP mode: DMR39-Bridge"));
        WiFi.mode(WIFI_AP);
        WiFi.softAP("DMR39-Bridge", "dmr39esp8266");
        wifiUp = true;
        Serial.printf("AP IP: %s\n", WiFi.softAPIP().toString().c_str());
    }
}

void handleSerialCommand() {
    if (!Serial.available()) return;
    char cmd = Serial.read();
    while (Serial.available()) Serial.read();
    switch (cmd) {
        case 'a': case 'A': {
            Serial.println(F("Capturing edges (2s)..."));
            detachKeyResponder();
            analyzer.begin(PIN_DAT, PIN_CLK);
            uint16_t n = analyzer.captureEdges(2000);
            Serial.printf("Got %u events\n", n);
            analyzer.printEdgeLog(Serial);
            if (currentMode == MODE_NORMAL) { emu.begin(PIN_DAT, PIN_CLK); setupKeyResponder(); }
            break;
        }
        case 'd': case 'D': {
            Serial.println(F("Capture + I2C decode (2s)..."));
            detachKeyResponder();
            analyzer.begin(PIN_DAT, PIN_CLK);
            analyzer.captureEdges(2000);
            analyzer.decodeI2C(Serial);
            if (currentMode == MODE_NORMAL) { emu.begin(PIN_DAT, PIN_CLK); setupKeyResponder(); }
            break;
        }
        case 'f': case 'F': {
            Serial.println(F("Measuring CLK (1s)..."));
            detachKeyResponder();
            analyzer.begin(PIN_DAT, PIN_CLK);
            uint32_t freq = analyzer.measureClockFreqHz(1000);
            Serial.printf("CLK: %lu Hz\n", (unsigned long)freq);
            if (freq == 0) Serial.println(F("No clock edges. Is STB powered on?"));
            if (currentMode == MODE_NORMAL) { emu.begin(PIN_DAT, PIN_CLK); setupKeyResponder(); }
            break;
        }
        case 's': case 'S':
            Serial.println(F("Continuous sniff (Enter to stop)..."));
            detachKeyResponder();
            analyzer.begin(PIN_DAT, PIN_CLK);
            currentMode = MODE_PASSTHROUGH;
            break;
        case 'n': case 'N':
            Serial.println(F("Normal mode — passive sniff + GPIO inject."));
            currentMode = MODE_NORMAL;
            break;
        case 'i':
            Serial.println(F("GPIO inject: CH_A (D6 HIGH) for 500ms"));
            startKeyInject(KEY_CH_A, 500);
            break;
        case 'I':
            Serial.println(F("GPIO inject: CH_A (D6 HIGH) for 2000ms"));
            startKeyInject(KEY_CH_A, 2000);
            break;
        case 'j':
            Serial.println(F("GPIO inject: CH_B (D7 HIGH) for 500ms"));
            startKeyInject(KEY_CH_B, 500);
            break;
        case 'J':
            Serial.println(F("GPIO inject: CH_B (D7 HIGH) for 2000ms"));
            startKeyInject(KEY_CH_B, 2000);
            break;
        case 'c': case 'C':
            Serial.println(F("Key cleared."));
            emu.clearKeyResponse();
            break;
        case 'w': case 'W':
            if (!wifiUp) { setupWiFi(); if (wifiUp && !httpUp) setupHTTP(); }
            else Serial.printf("WiFi up: %s\n", WiFi.getMode() == WIFI_STA ?
                WiFi.localIP().toString().c_str() : WiFi.softAPIP().toString().c_str());
            break;
        case 'q': case 'Q':
            wifiScan();
            break;
        case 'x': case 'X':
            Serial.printf("Mode: %s  KeyResp: %s\n",
                currentMode == MODE_NORMAL ? "normal" : "analyzer",
                kr_v4_active ? "V4-POLL" : "V3-ISR");
            Serial.printf("Stats: starts=%lu stops=%lu handled=%lu nack=%lu timeout=%lu\n",
                (unsigned long)kr_startCount, (unsigned long)kr_stopCount,
                (unsigned long)kr_handleCount, (unsigned long)kr_nackCount,
                (unsigned long)kr_timeoutCount);
            Serial.printf("KeyCode: 0x%02X  Inject: %s  LastAddr: 0x%02X\n", kr_keyCode,
                keyInject.active ? "ACTIVE" : "idle", kr_lastAddr);
            Serial.printf("AddrLog(%d):", kr_addrLogIdx);
            for (uint8_t i = 0; i < kr_addrLogIdx; i++) Serial.printf(" %02X", kr_addrLog[i]);
            Serial.println();
            Serial.printf("Writes: %lu  WriteLog(%d):", (unsigned long)kr_writeCount, kr_writeLogIdx);
            for (uint8_t i = 0; i < kr_writeLogIdx && i < 32; i++)
                Serial.printf(" [%02X]=%02X", kr_writeLog[i].addr, kr_writeLog[i].data);
            Serial.println();
            emu.printTrace(Serial);
            break;
        case 'p': case 'P': {
            // High-speed button matrix scan analyzer
            // Captures D5/D6/D7 digital + A0 analog at maximum rate
            // to understand HW650EP scanning pattern
            Serial.println(F("=== Button Matrix Scan Analyzer (500ms) ==="));
            Serial.println(F("Sampling D5(Pwr) D6(A) D7(B) A0 at max rate..."));
            pinMode(BTN_POWER, INPUT);
            pinMode(BTN_A, INPUT);
            pinMode(BTN_B, INPUT);

            struct ScanSample {
                uint32_t us;
                uint16_t a0;
                uint8_t d5, d6, d7;
            };
            static ScanSample sbuf[512];
            uint16_t scount = 0;

            // Capture on ANY change in digital pins or significant A0 change
            uint8_t prev5 = digitalRead(BTN_POWER);
            uint8_t prev6 = digitalRead(BTN_A);
            uint8_t prev7 = digitalRead(BTN_B);
            uint16_t prevA0 = analogRead(A0);
            uint32_t startUs = micros();
            uint32_t endUs = startUs + 500000;
            uint32_t lastFeed = millis();

            // First sample
            sbuf[0] = {0, prevA0, prev5, prev6, prev7};
            scount = 1;

            while (micros() < endUs && scount < 512) {
                uint8_t c5 = digitalRead(BTN_POWER);
                uint8_t c6 = digitalRead(BTN_A);
                uint8_t c7 = digitalRead(BTN_B);
                uint16_t cA0 = analogRead(A0);
                // Log on any digital change or A0 change > 50
                bool changed = (c5 != prev5) || (c6 != prev6) || (c7 != prev7);
                int16_t a0diff = (int16_t)cA0 - (int16_t)prevA0;
                if (a0diff < 0) a0diff = -a0diff;
                if (changed || a0diff > 50) {
                    sbuf[scount].us = micros() - startUs;
                    sbuf[scount].a0 = cA0;
                    sbuf[scount].d5 = c5;
                    sbuf[scount].d6 = c6;
                    sbuf[scount].d7 = c7;
                    scount++;
                    prev5 = c5; prev6 = c6; prev7 = c7; prevA0 = cA0;
                }
                if (millis() - lastFeed > 200) { ESP.wdtFeed(); lastFeed = millis(); }
            }
            Serial.printf("Captured %u events in 500ms\n", scount);
            Serial.println(F("  Time(us)   A0   D5  D6  D7"));
            for (uint16_t i = 0; i < scount && i < 200; i++) {
                Serial.printf("  %8lu  %4u   %d   %d   %d\n",
                    (unsigned long)sbuf[i].us, sbuf[i].a0,
                    sbuf[i].d5, sbuf[i].d6, sbuf[i].d7);
            }
            if (scount >= 200) Serial.printf("  ... (%u more)\n", scount - 200);
            Serial.println(F("=== End Scan ==="));
            break;
        }
        case 'k': case 'K': {
            // GPIO button state only — no Wire master (shared bus, would conflict)
            Serial.println(F("=== GPIO Button State ==="));
            Serial.printf("A0=%d  Pwr(D5)=%d A(D6)=%d B(D7)=%d\n",
                analogRead(A0), digitalRead(BTN_POWER), digitalRead(BTN_A), digitalRead(BTN_B));
            Serial.println(F("(Wire key scan disabled — STB reads front panel directly via blob bridge)"));
            break;
        }
        case '1': {
            // Inject D6 LOW (opposite of normal HIGH inject)
            Serial.println(F("TEST: D6 LOW for 500ms"));
            pinMode(BTN_A, OUTPUT); digitalWrite(BTN_A, LOW);
            delay(500);
            pinMode(BTN_A, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case '2': {
            // Inject D7 LOW
            Serial.println(F("TEST: D7 LOW for 500ms"));
            pinMode(BTN_B, OUTPUT); digitalWrite(BTN_B, LOW);
            delay(500);
            pinMode(BTN_B, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case '3': {
            // Inject D5 HIGH (opposite of normal LOW inject for power)
            Serial.println(F("TEST: D5 HIGH for 500ms"));
            pinMode(BTN_POWER, OUTPUT); digitalWrite(BTN_POWER, HIGH);
            delay(500);
            pinMode(BTN_POWER, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case '4': {
            // Inject ALL three LOW simultaneously
            Serial.println(F("TEST: D5+D6+D7 ALL LOW for 500ms"));
            pinMode(BTN_POWER, OUTPUT); digitalWrite(BTN_POWER, LOW);
            pinMode(BTN_A, OUTPUT); digitalWrite(BTN_A, LOW);
            pinMode(BTN_B, OUTPUT); digitalWrite(BTN_B, LOW);
            delay(500);
            pinMode(BTN_POWER, INPUT);
            pinMode(BTN_A, INPUT);
            pinMode(BTN_B, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case '5': {
            // Inject ALL three HIGH simultaneously
            Serial.println(F("TEST: D5+D6+D7 ALL HIGH for 500ms"));
            pinMode(BTN_POWER, OUTPUT); digitalWrite(BTN_POWER, HIGH);
            pinMode(BTN_A, OUTPUT); digitalWrite(BTN_A, HIGH);
            pinMode(BTN_B, OUTPUT); digitalWrite(BTN_B, HIGH);
            delay(500);
            pinMode(BTN_POWER, INPUT);
            pinMode(BTN_A, INPUT);
            pinMode(BTN_B, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case '6': {
            // Short A0 to GND via a button pin — simulate button connecting scan to GND
            // Drive D6 as open-drain LOW only (don't fight the chip)
            Serial.println(F("TEST: D6 open-drain LOW for 2s (don't fight chip)"));
            // Use GPEC to set as input (high-Z), GPES+GPOC to drive LOW
            GPES = (1 << BTN_A);  // set as output
            GPOC = (1 << BTN_A);  // drive LOW
            delay(2000);
            GPEC = (1 << BTN_A);  // back to input
            Serial.println(F("Released"));
            break;
        }
        case '7': {
            // Scan-synced inject: wait for D6 to go HIGH (idle), then pull LOW
            Serial.println(F("TEST: Scan-synced D6 LOW (10 cycles)"));
            uint8_t hits = 0;
            uint32_t deadline = millis() + 2000;
            while (hits < 10 && millis() < deadline) {
                // Wait for D6 HIGH (chip released the pin)
                while (!digitalRead(BTN_A) && millis() < deadline) {}
                // Now D6 is HIGH — this is the idle/key-scan window
                // Drive LOW to simulate button
                pinMode(BTN_A, OUTPUT);
                digitalWrite(BTN_A, LOW);
                delayMicroseconds(500);  // hold for 500us (within scan window)
                pinMode(BTN_A, INPUT);
                hits++;
                delay(10);  // wait for next scan cycle
            }
            Serial.printf("Done: %d synced injects\n", hits);
            break;
        }
        case '8': {
            // Simulate button by bridging D6↔A0: mirror A0 state onto D6
            // When A0 drops (scan pulse), D6 follows LOW (like button shorting them).
            // When A0 is high, D6 floats (input, no pullup to avoid interference).
            Serial.println(F("TEST: A0→D6 bridge (5s) — simulate button D6↔A0"));
            uint32_t deadline = millis() + 5000;
            uint32_t syncCount = 0;
            uint32_t loopCount = 0;
            uint32_t lastFeed = millis();
            while (millis() < deadline) {
                uint16_t a0 = analogRead(A0);
                if (a0 < 200) {
                    GPES = (1 << BTN_A); GPOC = (1 << BTN_A);  // output LOW
                    syncCount++;
                } else {
                    GPEC = (1 << BTN_A);  // back to input (float)
                }
                loopCount++;
                if (millis() - lastFeed > 100) {
                    ESP.wdtFeed();
                    yield();
                    lastFeed = millis();
                }
            }
            GPEC = (1 << BTN_A);
            pinMode(BTN_A, INPUT);  // float, no pullup
            Serial.printf("Done: %lu synced/%lu total loops\n",
                (unsigned long)syncCount, (unsigned long)loopCount);
            break;
        }
        case 'b': case 'B': {
            // Same bridge for D7 (BTN_B)
            Serial.println(F("TEST: A0→D7 bridge (5s) — simulate button D7↔A0"));
            uint32_t deadline = millis() + 5000;
            uint32_t syncCount = 0;
            uint32_t loopCount = 0;
            uint32_t lastFeed = millis();
            while (millis() < deadline) {
                uint16_t a0 = analogRead(A0);
                if (a0 < 200) {
                    GPES = (1 << BTN_B); GPOC = (1 << BTN_B);
                    syncCount++;
                } else {
                    GPEC = (1 << BTN_B);
                }
                loopCount++;
                if (millis() - lastFeed > 100) {
                    ESP.wdtFeed();
                    yield();
                    lastFeed = millis();
                }
            }
            GPEC = (1 << BTN_B);
            pinMode(BTN_B, INPUT);
            Serial.printf("Done: %lu synced/%lu total loops\n",
                (unsigned long)syncCount, (unsigned long)loopCount);
            break;
        }
        case 'g': case 'G': {
            // Power button: D5 LOW for 2s (user priority — detectable via HDMI)
            Serial.println(F("TEST: POWER (D5 LOW, 2s)"));
            pinMode(BTN_POWER, OUTPUT);
            digitalWrite(BTN_POWER, LOW);
            uint32_t tw = millis();
            while (millis() - tw < 2000) { ESP.wdtFeed(); yield(); }
            pinMode(BTN_POWER, INPUT);  // float, no pullup
            Serial.println(F("Released"));
            break;
        }
        case 'l': case 'L': {
            // Long hold test: D6 HIGH for 10s (give STB plenty of time to poll)
            Serial.println(F("TEST: D6 HIGH (10s) — long hold for STB polling"));
            pinMode(BTN_A, OUTPUT);
            digitalWrite(BTN_A, HIGH);
            uint32_t tw = millis();
            while (millis() - tw < 10000) { ESP.wdtFeed(); yield(); }
            pinMode(BTN_A, INPUT);
            Serial.println(F("Released"));
            break;
        }
        case 'r': case 'R': {
            // Rapid repeated short pulses on D6 HIGH — like repeatedly tapping a button
            Serial.println(F("TEST: D6 rapid pulses (20x 100ms HIGH, 100ms off) over 4s"));
            for (int i = 0; i < 20; i++) {
                pinMode(BTN_A, OUTPUT);
                digitalWrite(BTN_A, HIGH);
                delay(100);
                pinMode(BTN_A, INPUT);
                delay(100);
                ESP.wdtFeed();
            }
            Serial.println(F("Done"));
            break;
        }
        case '9': {
            // Phase-specific D6 injection: try each idle window separately
            // The scan cycle has 3 idle windows (D6 HIGH) between display phases.
            // We inject LOW during only ONE window per cycle to avoid multi-key lockout.
            // Use deep scan (A0 < 10) as cycle boundary marker.
            Serial.println(F("=== PHASE-SPECIFIC D6 LOW INJECTION ==="));
            Serial.println(F("Testing each idle window (1,2,3) for 3s each..."));

            floatAllButtons();  // ensure clean state

            for (int phase = 1; phase <= 3; phase++) {
                Serial.printf("\n--- Phase %d: inject D6 LOW during idle window #%d ---\n", phase, phase);
                uint32_t deadline = millis() + 3000;
                uint32_t injectCount = 0;
                uint32_t cycleCount = 0;
                uint32_t lastFeed = millis();

                while (millis() < deadline) {
                    // Wait for deep scan (cycle boundary): A0 < 10 AND D6 LOW AND D7 LOW
                    while (analogRead(A0) >= 10 && millis() < deadline) {
                        if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                    }
                    if (millis() >= deadline) break;
                    // Wait for deep scan to end (A0 > 500)
                    while (analogRead(A0) < 500 && millis() < deadline) {}
                    if (millis() >= deadline) break;
                    cycleCount++;

                    // Now count D6 rising edges (LOW→HIGH transitions) to find the right idle window
                    int windowsSeen = 0;
                    uint32_t windowDeadline = micros() + 20000; // max 20ms per cycle
                    uint8_t prevD6 = digitalRead(BTN_A);

                    while (windowsSeen < phase && micros() < windowDeadline) {
                        uint8_t curD6 = digitalRead(BTN_A);
                        if (prevD6 == 0 && curD6 == 1) {
                            windowsSeen++;
                        }
                        prevD6 = curD6;
                    }

                    // We're now at the start of idle window #phase
                    // Drive D6 LOW for 2ms (within the ~5ms window)
                    if (windowsSeen == phase) {
                        GPES = (1 << BTN_A);
                        GPOC = (1 << BTN_A);  // output LOW
                        delayMicroseconds(2000);
                        GPEC = (1 << BTN_A);  // back to input (float)
                        injectCount++;
                    }

                    if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                }
                Serial.printf("Phase %d: %lu injects in %lu cycles\n",
                    phase, (unsigned long)injectCount, (unsigned long)cycleCount);
            }
            pinMode(BTN_A, INPUT);
            Serial.println(F("=== Done ==="));
            break;
        }
        case '0': {
            // Same phase-specific injection but with D6 HIGH instead of LOW
            // (In case the key detection polarity is inverted)
            Serial.println(F("=== PHASE-SPECIFIC D6 HIGH INJECTION ==="));
            Serial.println(F("Testing each idle window (1,2,3) for 3s each..."));

            floatAllButtons();

            for (int phase = 1; phase <= 3; phase++) {
                Serial.printf("\n--- Phase %d: inject D6 HIGH during idle window #%d ---\n", phase, phase);
                uint32_t deadline = millis() + 3000;
                uint32_t injectCount = 0;
                uint32_t cycleCount = 0;
                uint32_t lastFeed = millis();

                while (millis() < deadline) {
                    while (analogRead(A0) >= 10 && millis() < deadline) {
                        if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                    }
                    if (millis() >= deadline) break;
                    while (analogRead(A0) < 500 && millis() < deadline) {}
                    if (millis() >= deadline) break;
                    cycleCount++;

                    int windowsSeen = 0;
                    uint32_t windowDeadline = micros() + 20000;
                    uint8_t prevD6 = digitalRead(BTN_A);

                    while (windowsSeen < phase && micros() < windowDeadline) {
                        uint8_t curD6 = digitalRead(BTN_A);
                        if (prevD6 == 0 && curD6 == 1) {
                            windowsSeen++;
                        }
                        prevD6 = curD6;
                    }

                    if (windowsSeen == phase) {
                        // Drive HIGH during this specific window
                        GPES = (1 << BTN_A);
                        GPOS = (1 << BTN_A);  // output HIGH
                        delayMicroseconds(2000);
                        GPEC = (1 << BTN_A);  // back to float
                        injectCount++;
                    }

                    if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                }
                Serial.printf("Phase %d: %lu injects in %lu cycles\n",
                    phase, (unsigned long)injectCount, (unsigned long)cycleCount);
            }
            pinMode(BTN_A, INPUT);
            Serial.println(F("=== Done ==="));
            break;
        }
        case 'e': case 'E': {
            // Exhaustive test: try ALL 6 combinations (3 phases × LOW/HIGH) on D6
            // for 5s each, with HDMI snapshot timestamps
            Serial.println(F("=== EXHAUSTIVE D6 PHASE TEST (6 combos, 5s each = 30s) ==="));
            floatAllButtons();

            for (int polarity = 0; polarity <= 1; polarity++) {
                for (int phase = 1; phase <= 3; phase++) {
                    const char* pol = polarity ? "HIGH" : "LOW";
                    Serial.printf("\nMARK_START phase=%d polarity=%s uptime=%lu\n",
                        phase, pol, millis());

                    uint32_t deadline = millis() + 5000;
                    uint32_t injectCount = 0;
                    uint32_t lastFeed = millis();

                    while (millis() < deadline) {
                        // Wait for deep scan
                        while (analogRead(A0) >= 10 && millis() < deadline) {
                            if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                        }
                        if (millis() >= deadline) break;
                        while (analogRead(A0) < 500 && millis() < deadline) {}
                        if (millis() >= deadline) break;

                        // Count D6 rising edges to target window
                        int windowsSeen = 0;
                        uint32_t windowDeadline = micros() + 20000;
                        uint8_t prevD6 = digitalRead(BTN_A);
                        while (windowsSeen < phase && micros() < windowDeadline) {
                            uint8_t curD6 = digitalRead(BTN_A);
                            if (prevD6 == 0 && curD6 == 1) windowsSeen++;
                            prevD6 = curD6;
                        }

                        if (windowsSeen == phase) {
                            GPES = (1 << BTN_A);
                            if (polarity) GPOS = (1 << BTN_A); else GPOC = (1 << BTN_A);
                            delayMicroseconds(2000);
                            GPEC = (1 << BTN_A);
                            injectCount++;
                        }
                        if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
                    }
                    Serial.printf("MARK_END phase=%d polarity=%s injects=%lu uptime=%lu\n",
                        phase, pol, (unsigned long)injectCount, millis());
                }
            }
            pinMode(BTN_A, INPUT);
            Serial.println(F("\n=== EXHAUSTIVE TEST COMPLETE ==="));
            break;
        }
        case 'v': case 'V': {
            // Verify GPIO connection: scan WHILE injecting D6 HIGH
            // If D6 stays 1 throughout, wire is connected and drive works
            Serial.println(F("=== GPIO CONNECTION VERIFY ==="));
            Serial.println(F("--- Baseline (no inject) ---"));
            floatAllButtons();
            {
                uint8_t d6_hi = 0, d6_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_A)) d6_hi++; else d6_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D6: %u HIGH, %u LOW (should toggle)\n", d6_hi, d6_lo);
            }
            Serial.println(F("--- D6 OUTPUT HIGH ---"));
            pinMode(BTN_A, OUTPUT);
            digitalWrite(BTN_A, HIGH);
            {
                uint8_t d6_hi = 0, d6_lo = 0;
                uint16_t a0_min = 1024, a0_max = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_A)) d6_hi++; else d6_lo++;
                    uint16_t a0 = analogRead(A0);
                    if (a0 < a0_min) a0_min = a0;
                    if (a0 > a0_max) a0_max = a0;
                    delayMicroseconds(20);
                }
                Serial.printf("D6: %u HIGH, %u LOW (should be all HIGH if connected)\n", d6_hi, d6_lo);
                Serial.printf("A0: min=%u max=%u (scan pulses still present?)\n", a0_min, a0_max);
            }
            Serial.println(F("--- D6 OUTPUT LOW ---"));
            digitalWrite(BTN_A, LOW);
            {
                uint8_t d6_hi = 0, d6_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_A)) d6_hi++; else d6_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D6: %u HIGH, %u LOW (should be all LOW if connected)\n", d6_hi, d6_lo);
            }
            pinMode(BTN_A, INPUT);
            Serial.println(F("--- D7 verify ---"));
            Serial.println(F("Baseline:"));
            {
                uint8_t d7_hi = 0, d7_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_B)) d7_hi++; else d7_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D7: %u HIGH, %u LOW\n", d7_hi, d7_lo);
            }
            pinMode(BTN_B, OUTPUT);
            digitalWrite(BTN_B, HIGH);
            {
                uint8_t d7_hi = 0, d7_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_B)) d7_hi++; else d7_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D7 HIGH: %u HIGH, %u LOW\n", d7_hi, d7_lo);
            }
            pinMode(BTN_B, INPUT);
            Serial.println(F("--- D5 verify ---"));
            {
                uint8_t d5_hi = 0, d5_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_POWER)) d5_hi++; else d5_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D5 baseline: %u HIGH, %u LOW\n", d5_hi, d5_lo);
            }
            pinMode(BTN_POWER, OUTPUT);
            digitalWrite(BTN_POWER, HIGH);
            {
                uint8_t d5_hi = 0, d5_lo = 0;
                for (int i = 0; i < 1000; i++) {
                    if (digitalRead(BTN_POWER)) d5_hi++; else d5_lo++;
                    delayMicroseconds(20);
                }
                Serial.printf("D5 HIGH: %u HIGH, %u LOW\n", d5_hi, d5_lo);
            }
            pinMode(BTN_POWER, INPUT);
            floatAllButtons();
            Serial.println(F("=== End ==="));
            break;
        }
        case 'z': case 'Z': {
            // Direct opposite: instead of driving D6, SHORT D6 to A0
            // by driving BOTH pins to the same state simultaneously.
            // This is different from driving D6 alone — it simulates
            // the physical button connection.
            // We use D6 as OUTPUT LOW when A0 is LOW,
            // and D6 as INPUT (float) when A0 is HIGH.
            // But also monitor D6's own idle/active state to only
            // inject during key scan windows (D6 was HIGH before we drove it).
            Serial.println(F("=== D6 scan-window LOW (5s) — only when D6 naturally HIGH ==="));
            floatAllButtons();
            uint32_t deadline = millis() + 5000;
            uint32_t injectCount = 0;
            uint32_t lastFeed = millis();
            while (millis() < deadline) {
                // Check both D6 state and A0
                uint8_t d6 = digitalRead(BTN_A);
                uint16_t a0 = analogRead(A0);
                if (d6 == 1 && a0 < 200) {
                    // D6 is HIGH (chip released it for key scan) AND A0 is LOW (scan active)
                    // Drive D6 LOW to simulate button press
                    GPES = (1 << BTN_A);
                    GPOC = (1 << BTN_A);
                    delayMicroseconds(200);  // brief pulse
                    GPEC = (1 << BTN_A);
                    injectCount++;
                }
                if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
            }
            pinMode(BTN_A, INPUT);
            Serial.printf("Done: %lu scan-window injects\n", (unsigned long)injectCount);
            break;
        }
        case 'M': case 'm': {
            // NEW APPROACH: D6 is a DIG scan OUTPUT from HW650EP (toggles LOW/HIGH).
            // D7 might be a KI (key input) for CH_B.
            // Physical button shorts DIG to KI. Simulate CH_B by pulling D7 LOW
            // ONLY when D6 is LOW (DIG scan active).
            // D6 must be floating (INPUT) so HW650EP drives it naturally.
            Serial.println(F("=== D6→D7 MIRROR (5s) — simulate CH_B (DIG→KI short) ==="));
            floatAllButtons();

            uint32_t deadline = millis() + 5000;
            uint32_t syncCount = 0;
            uint32_t loopCount = 0;
            uint32_t lastFeed = millis();
            while (millis() < deadline) {
                if (!(GPI & (1 << BTN_A))) {
                    // D6 is LOW (DIG scan active) — pull D7 LOW (simulate button)
                    GPES = (1 << BTN_B);  // enable output
                    GPOC = (1 << BTN_B);  // drive LOW
                    syncCount++;
                } else {
                    // D6 is HIGH (DIG inactive) — float D7
                    GPEC = (1 << BTN_B);  // disable output (float)
                }
                loopCount++;
                if (millis() - lastFeed > 100) { ESP.wdtFeed(); yield(); lastFeed = millis(); }
            }
            GPEC = (1 << BTN_B);
            pinMode(BTN_B, INPUT);
            floatAllButtons();
            Serial.printf("Done: %lu synced/%lu total loops\n",
                (unsigned long)syncCount, (unsigned long)loopCount);
            break;
        }
        case 'Y': case 'y': {
            // REVERSE: Pull D7 LOW continuously (5s) while D6 floats.
            // Tests if continuous KI LOW works (vs scan-synced).
            Serial.println(F("=== D7 CONTINUOUS LOW (5s) — D6 floating ==="));
            floatAllButtons();
            pinMode(BTN_B, OUTPUT);
            digitalWrite(BTN_B, LOW);
            {
                uint32_t tw = millis();
                while (millis() - tw < 5000) { ESP.wdtFeed(); yield(); }
            }
            floatAllButtons();
            Serial.println(F("Released"));
            break;
        }
        case 'O': case 'o': {
            // D2 (GPIO4) = TM1650 KI pin DIRECTLY (past series resistor)
            // A0 (ADC)   = common pad (button side of resistor) — analog timing ref
            // D6 (GPIO12)= DIG1 pin — digital timing ref
            // SAFETY: D2 floats except during active injection.
            #define KI_PIN  4   // GPIO4 = D2
            #define DIG_PIN 12  // GPIO12 = D6
            #define CYCLES_PER_US 160  // 160MHz CPU

            // Float everything first
            GPEC = (1 << KI_PIN);
            pinMode(KI_PIN, INPUT);
            PIN_PULLUP_DIS(PERIPHS_IO_MUX_GPIO4_U);
            floatAllButtons();

            Serial.println(F("D2=KI(direct), A0=common, D6=DIG1. All floating."));

            // === Phase 0: Verify D2 can drive KI — check via A0 ===
            Serial.println(F("--- Phase 0: Drive verify via A0 ---"));
            {
                int a0_idle = analogRead(A0);
                // Drive D2 HIGH briefly
                GPOS = (1 << KI_PIN);
                GPES = (1 << KI_PIN);
                delayMicroseconds(100);
                int a0_hi = analogRead(A0);
                GPEC = (1 << KI_PIN);  // float
                delayMicroseconds(100);
                // Drive D2 LOW briefly
                GPOC = (1 << KI_PIN);
                GPES = (1 << KI_PIN);
                delayMicroseconds(100);
                int a0_lo = analogRead(A0);
                GPEC = (1 << KI_PIN);  // float
                Serial.printf("A0 idle=%d, D2_HIGH=%d, D2_LOW=%d\n", a0_idle, a0_hi, a0_lo);
                if (a0_hi == a0_idle && a0_lo == a0_idle)
                    Serial.println(F("WARNING: A0 unchanged — D2 may not be connected to circuit!"));
                else
                    Serial.println(F("OK: D2 drive changes A0 — connected through resistor."));
            }

            // === Phase 1: A0 scan characterization (500ms) ===
            Serial.println(F("--- Phase 1: A0+D6 scan timing (500ms) ---"));
            {
                // Sample A0 and D6 together, find pattern
                uint32_t t0 = millis();
                int minA0 = 1024, maxA0 = 0;
                uint32_t d6hi = 0, d6lo = 0, samples = 0;
                // Also time D6 edges precisely
                uint32_t d6edges = 0;
                uint8_t lastD6 = (GPI >> DIG_PIN) & 1;
                uint32_t lastEdgeCyc = ESP.getCycleCount();
                uint32_t minHiCyc = 0xFFFFFFFF, maxHiCyc = 0;
                uint32_t minLoCyc = 0xFFFFFFFF, maxLoCyc = 0;

                while (millis() - t0 < 500) {
                    int a = analogRead(A0);
                    uint8_t d6 = (GPI >> DIG_PIN) & 1;
                    if (a < minA0) minA0 = a;
                    if (a > maxA0) maxA0 = a;
                    if (d6) d6hi++; else d6lo++;
                    samples++;

                    if (d6 != lastD6) {
                        uint32_t now = ESP.getCycleCount();
                        uint32_t dt = now - lastEdgeCyc;
                        if (lastD6) { // was HIGH, now LOW
                            if (dt < minHiCyc) minHiCyc = dt;
                            if (dt > maxHiCyc) maxHiCyc = dt;
                        } else { // was LOW, now HIGH
                            if (dt < minLoCyc) minLoCyc = dt;
                            if (dt > maxLoCyc) maxLoCyc = dt;
                        }
                        lastEdgeCyc = now;
                        d6edges++;
                        lastD6 = d6;
                    }
                }
                Serial.printf("A0: min=%d max=%d | D6: hi=%lu lo=%lu edges=%lu samples=%lu\n",
                    minA0, maxA0, (unsigned long)d6hi, (unsigned long)d6lo,
                    (unsigned long)d6edges, (unsigned long)samples);
                if (d6edges > 2) {
                    Serial.printf("D6 HIGH: %lu-%lu us | LOW: %lu-%lu us\n",
                        (unsigned long)(minHiCyc / CYCLES_PER_US),
                        (unsigned long)(maxHiCyc / CYCLES_PER_US),
                        (unsigned long)(minLoCyc / CYCLES_PER_US),
                        (unsigned long)(maxLoCyc / CYCLES_PER_US));
                }
            }

            // === Mode 1: ULTRA-TIGHT D6-synced injection (5s) ===
            // Interrupts DISABLED during injection bursts for minimum latency.
            // Detect D6 rising edge → immediately drive KI HIGH.
            // Detect D6 falling edge → immediately float KI.
            Serial.println(F("=== MODE 1: ULTRA-TIGHT D6-synced KI HIGH (5s) ==="));
            {
                // Pre-configure: set GPIO4 output value to HIGH before enabling
                GPOS = (1 << KI_PIN);  // latch HIGH value
                uint32_t startCyc = ESP.getCycleCount();
                uint32_t durationCyc = (uint32_t)5 * 160000000UL; // 5 seconds
                uint32_t driveCount = 0;
                uint32_t feedCounter = 0;
                uint8_t lastD6 = (GPI >> DIG_PIN) & 1;

                while ((ESP.getCycleCount() - startCyc) < durationCyc) {
                    uint32_t gpi = GPI;
                    uint8_t d6 = (gpi >> DIG_PIN) & 1;

                    if (d6 && !lastD6) {
                        // D6 rising edge → scan phase started
                        // Disable interrupts for fastest possible response
                        ets_intr_lock();
                        GPES = (1 << KI_PIN);  // enable output (already latched HIGH)
                        ets_intr_unlock();
                        driveCount++;
                    } else if (!d6 && lastD6) {
                        // D6 falling edge → display phase, float immediately
                        ets_intr_lock();
                        GPEC = (1 << KI_PIN);  // disable output (float)
                        ets_intr_unlock();
                    }
                    lastD6 = d6;

                    if (++feedCounter >= 100000) {
                        ESP.wdtFeed();
                        feedCounter = 0;
                    }
                }
                GPEC = (1 << KI_PIN);
                Serial.printf("Rising edges caught: %lu\n", (unsigned long)driveCount);
            }
            Serial.println(F("Done mode 1"));
            delay(3000);

            // === Mode 2: ULTRA-TIGHT with DELAYED injection (5s) ===
            // Maybe scan sampling happens partway through the DIG HIGH phase.
            // Drive KI HIGH starting 200us after D6 rising edge.
            Serial.println(F("=== MODE 2: D6-synced + 200us delay (5s) ==="));
            {
                GPOS = (1 << KI_PIN);
                uint32_t startCyc = ESP.getCycleCount();
                uint32_t durationCyc = (uint32_t)5 * 160000000UL;
                uint32_t driveCount = 0, feedCounter = 0;
                uint8_t lastD6 = (GPI >> DIG_PIN) & 1;
                uint32_t delayCycles = 200 * CYCLES_PER_US; // 200us

                while ((ESP.getCycleCount() - startCyc) < durationCyc) {
                    uint32_t gpi = GPI;
                    uint8_t d6 = (gpi >> DIG_PIN) & 1;

                    if (d6 && !lastD6) {
                        // Wait 200us then drive
                        uint32_t target = ESP.getCycleCount() + delayCycles;
                        while (ESP.getCycleCount() < target) {}
                        ets_intr_lock();
                        GPES = (1 << KI_PIN);
                        ets_intr_unlock();
                        driveCount++;
                    } else if (!d6 && lastD6) {
                        ets_intr_lock();
                        GPEC = (1 << KI_PIN);
                        ets_intr_unlock();
                    }
                    lastD6 = d6;

                    if (++feedCounter >= 100000) { ESP.wdtFeed(); feedCounter = 0; }
                }
                GPEC = (1 << KI_PIN);
                Serial.printf("Delayed injections: %lu\n", (unsigned long)driveCount);
            }
            Serial.println(F("Done mode 2"));
            delay(3000);

            // === Mode 3: A0-threshold synced (5s) ===
            // Use A0 analog reading to detect scan phase on common pad.
            // When A0 drops below threshold → scan window → drive KI HIGH.
            Serial.println(F("=== MODE 3: A0-threshold synced KI HIGH (5s) ==="));
            {
                // Quick calibration: read A0 for 50ms
                int a0sum = 0, a0n = 0;
                uint32_t cal = millis();
                while (millis() - cal < 50) {
                    a0sum += analogRead(A0);
                    a0n++;
                }
                int a0avg = a0sum / a0n;
                int threshold = a0avg / 2;  // trigger when drops to half
                Serial.printf("A0 avg=%d, threshold=%d\n", a0avg, threshold);

                GPOS = (1 << KI_PIN);
                uint32_t deadline = millis() + 5000;
                uint32_t driveCount = 0, feedCounter = 0;
                bool driving = false;

                while (millis() < deadline) {
                    int a0 = analogRead(A0);
                    if (a0 < threshold && !driving) {
                        // Scan window detected
                        GPES = (1 << KI_PIN);
                        driving = true;
                        driveCount++;
                    } else if (a0 >= threshold && driving) {
                        GPEC = (1 << KI_PIN);
                        driving = false;
                    }
                    if (++feedCounter >= 1000) { ESP.wdtFeed(); feedCounter = 0; }
                }
                GPEC = (1 << KI_PIN);
                Serial.printf("A0-triggered drives: %lu\n", (unsigned long)driveCount);
            }
            Serial.println(F("Done mode 3"));
            delay(3000);

            // === Mode 4: Brute force continuous HIGH (5s) ===
            Serial.println(F("=== MODE 4: KI CONTINUOUS HIGH (5s) ==="));
            {
                PIN_FUNC_SELECT(PERIPHS_IO_MUX_GPIO4_U, FUNC_GPIO4);
                PIN_PULLUP_DIS(PERIPHS_IO_MUX_GPIO4_U);
                uint32_t pinReg = GPIO_REG_READ(GPIO_PIN_ADDR(KI_PIN));
                pinReg &= ~GPIO_PIN_PAD_DRIVER_MASK;
                GPIO_REG_WRITE(GPIO_PIN_ADDR(KI_PIN), pinReg);
                GPOS = (1 << KI_PIN);
                GPES = (1 << KI_PIN);
                uint32_t deadline = millis() + 5000;
                while (millis() < deadline) { ESP.wdtFeed(); yield(); }
                GPEC = (1 << KI_PIN);
            }
            Serial.println(F("Done mode 4"));

            // Cleanup
            GPEC = (1 << KI_PIN);
            pinMode(KI_PIN, INPUT);
            PIN_PULLUP_DIS(PERIPHS_IO_MUX_GPIO4_U);
            floatAllButtons();
            Serial.println(F("=== KI DIRECT INJECTION COMPLETE ==="));
            #undef KI_PIN
            #undef DIG_PIN
            #undef CYCLES_PER_US
            break;
        }
        case 't': case 'T': {
            // Auto-test sequence: try each button with delays, print timestamps
            // Check HDMI ring buffer after to see which caused a change
            Serial.println(F("=== AUTO-TEST SEQUENCE (I2C reconnected) ==="));
            Serial.printf("Uptime: %lu ms\n", millis());

            // Test 1: POWER (D5 LOW) — should toggle standby, HDMI goes dark
            Serial.println(F("[T+0s] Testing POWER (D5 LOW, 500ms)..."));
            Serial.printf("MARK_POWER_START %lu\n", millis());
            startKeyInject(KEY_STANDBY, 500);
            // Wait for inject to complete + STB reaction time
            uint32_t tw = millis();
            while (millis() - tw < 8000) { pollKeyInject(); yield(); }
            Serial.printf("MARK_POWER_END %lu\n", millis());

            // Test 2: POWER again to wake up (if it went to standby)
            Serial.println(F("[T+8s] Testing POWER again (wake up)..."));
            Serial.printf("MARK_POWER2_START %lu\n", millis());
            startKeyInject(KEY_STANDBY, 500);
            tw = millis();
            while (millis() - tw < 10000) { pollKeyInject(); yield(); }
            Serial.printf("MARK_POWER2_END %lu\n", millis());

            // Test 3: CH_A (D6 HIGH) — should change channel, blue OSD
            Serial.println(F("[T+18s] Testing CH_A (D6 HIGH, 500ms)..."));
            Serial.printf("MARK_CHA_START %lu\n", millis());
            startKeyInject(KEY_CH_A, 500);
            tw = millis();
            while (millis() - tw < 8000) { pollKeyInject(); yield(); }
            Serial.printf("MARK_CHA_END %lu\n", millis());

            // Test 4: CH_B (D7 HIGH) — should change channel other direction
            Serial.println(F("[T+26s] Testing CH_B (D7 HIGH, 500ms)..."));
            Serial.printf("MARK_CHB_START %lu\n", millis());
            startKeyInject(KEY_CH_B, 500);
            tw = millis();
            while (millis() - tw < 8000) { pollKeyInject(); yield(); }
            Serial.printf("MARK_CHB_END %lu\n", millis());

            Serial.println(F("=== AUTO-TEST COMPLETE ==="));
            Serial.printf("Total time: ~34s. Check HDMI ring buffer for changes.\n");
            break;
        }
        case 'h': case 'H': case '?':
            Serial.println(F("=== GPIO Button Inject Mode ==="));
            Serial.println(F("Sniff: a=edges d=decode f=freq s=sniff p=scan-analyzer"));
            Serial.println(F("Inject: i/I=D6 HIGH(short/long) j/J=D7 HIGH(short/long)"));
            Serial.println(F("Test:  1=D6 LOW  2=D7 LOW  3=D5 HIGH  4=all LOW  5=all HIGH"));
            Serial.println(F("       6=D6 open-drain  7=scan-sync D6  8=A0-sync D6"));
            Serial.println(F("       8=A0→D6 bridge  b=A0→D7 bridge  g=POWER(D5 LOW 2s)"));
            Serial.println(F("       l=D6 HIGH 10s  r=D6 rapid  t=AUTO-TEST  k=GPIO-state"));
            Serial.println(F("Mode:  n=normal x=stats w=wifi"));
            break;
        case '\n': case '\r':
            if (currentMode == MODE_PASSTHROUGH) {
                currentMode = MODE_NORMAL;
                emu.begin(PIN_DAT, PIN_CLK);
                Serial.println(F("\nSniff stopped."));
            }
            break;
    }
}
