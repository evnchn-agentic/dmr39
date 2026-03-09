// HW650EP Key Reader — standalone front panel key code extractor
// ESP8266 acts as I2C master to the HW650EP front panel.
// Displays "----" and reports button press key codes via serial.
//
// Wiring: D4(GPIO2)=DAT(SDA), D3(GPIO0)=CLK(SCL), 3V3=VDD, GND=GND
// Build: pio run -e keyreader
// Flash: pio run -e keyreader -t upload

#include <Arduino.h>
#include <Wire.h>

#define FP_SDA  2   // GPIO2 = D4 → front panel DAT
#define FP_SCL  0   // GPIO0 = D3 → front panel CLK

// TM1650/HW650EP 8-bit wire addresses
// Control: 0x48, Display: 0x68/0x6A/0x6C/0x6E, Key: 0x49/0x4F

bool fpWrite(uint8_t addr8, uint8_t data) {
    Wire.beginTransmission(addr8 >> 1);
    Wire.write(data);
    return Wire.endTransmission() == 0;
}

uint8_t fpRead(uint8_t addr8) {
    Wire.requestFrom((uint8_t)(addr8 >> 1), (uint8_t)1);
    if (Wire.available()) return Wire.read();
    return 0;
}

// Segment encoding for 7-segment display (0-F hex)
static const uint8_t SEG_HEX[] = {
    0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07,  // 0-7
    0x7F, 0x6F, 0x77, 0x7C, 0x39, 0x5E, 0x79, 0x71   // 8-F (A,b,C,d,E,F)
};
#define SEG_DASH 0x40
#define SEG_BLANK 0x00

void fpDisplay(uint8_t d1, uint8_t d2, uint8_t d3, uint8_t d4) {
    fpWrite(0x68, d1);
    fpWrite(0x6A, d2);
    fpWrite(0x6C, d3);
    fpWrite(0x6E, d4);
}

void fpDisplayStr(const char* s) {
    uint8_t segs[4] = {SEG_BLANK, SEG_BLANK, SEG_BLANK, SEG_BLANK};
    for (int i = 0; i < 4 && s[i]; i++) {
        char c = s[i];
        if (c >= '0' && c <= '9') segs[i] = SEG_HEX[c - '0'];
        else if (c >= 'A' && c <= 'F') segs[i] = SEG_HEX[c - 'A' + 10];
        else if (c >= 'a' && c <= 'f') segs[i] = SEG_HEX[c - 'a' + 10];
        else if (c == '-') segs[i] = SEG_DASH;
        else if (c == ' ') segs[i] = SEG_BLANK;
    }
    fpDisplay(segs[0], segs[1], segs[2], segs[3]);
}

uint8_t lastKey49 = 0;
uint8_t lastKey4F = 0;
uint32_t lastKeyTime = 0;
uint32_t keyCount = 0;

void setup() {
    Serial.begin(115200);
    delay(100);
    Serial.println(F("\n=== HW650EP Key Reader ==="));
    Serial.println(F("D4(GPIO2)=DAT  D3(GPIO0)=CLK"));

    Wire.begin(FP_SDA, FP_SCL);
    Wire.setClock(50000);  // 50kHz

    // I2C scan
    Serial.println(F("I2C scan:"));
    int found = 0;
    for (uint8_t addr = 0x20; addr <= 0x3F; addr++) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) {
            Serial.printf("  0x%02X (wire 0x%02X)\n", addr, addr << 1);
            found++;
        }
    }
    Serial.printf("Found %d addresses\n", found);

    if (found == 0) {
        Serial.println(F("No I2C devices! Check wiring. Trying swapped SDA/SCL..."));
        Wire.begin(FP_SCL, FP_SDA);  // try swapped
        Wire.setClock(50000);
        for (uint8_t addr = 0x20; addr <= 0x3F; addr++) {
            Wire.beginTransmission(addr);
            if (Wire.endTransmission() == 0) {
                Serial.printf("  SWAPPED: 0x%02X found!\n", addr);
                found++;
            }
        }
        if (found > 0) {
            Serial.println(F("Swapped pins work! Using GPIO0=SDA, GPIO2=SCL"));
        } else {
            Serial.println(F("Still nothing. Check wiring and power."));
        }
    }

    // Display ON: 0x41 = ON + 8seg + brightness 1
    bool ok = fpWrite(0x48, 0x41);
    Serial.printf("Display ctrl: %s\n", ok ? "OK" : "FAIL");

    // Cycle 0000→FFFF (16 hex digits, all 4 identical)
    Serial.println(F("Cycling hex digits 0-F..."));
    const char* hexChars = "0123456789ABCDEF";
    for (int h = 0; h < 16; h++) {
        uint8_t seg = SEG_HEX[h];
        fpDisplay(seg, seg, seg, seg);
        Serial.printf("  %c%c%c%c (seg=0x%02X)\n", hexChars[h], hexChars[h], hexChars[h], hexChars[h], seg);
        delay(1500);
    }
    // Back to dashes
    fpDisplayStr("----");
    Serial.println(F("Display: ---- (cycle done)"));
    Serial.println(F("Press buttons on the front panel..."));
    Serial.println();
}

void loop() {
    // Poll both key read addresses
    uint8_t k49 = fpRead(0x49);
    uint8_t k4F = fpRead(0x4F);

    bool changed = (k49 != lastKey49) || (k4F != lastKey4F);
    bool anyPressed = (k49 != 0) || (k4F != 0);

    if (changed && anyPressed) {
        keyCount++;
        Serial.printf("[%lu] KEY #%lu  0x49=0x%02X  0x4F=0x%02X\n",
                      millis(), keyCount, k49, k4F);
        lastKeyTime = millis();
    } else if (changed && !anyPressed && lastKeyTime > 0) {
        Serial.printf("[%lu] RELEASE (held %lums)\n",
                      millis(), millis() - lastKeyTime);
    }

    lastKey49 = k49;
    lastKey4F = k4F;

    delay(20);  // 50Hz poll
}
