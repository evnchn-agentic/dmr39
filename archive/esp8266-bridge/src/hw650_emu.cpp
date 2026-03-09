#include "hw650_emu.h"

// ── I2C Slave Emulator for HW650EP — V10 STUB ──
//
// The real-time I2C slave emulation has been unreliable due to signal noise.
// This version is a minimal stub that provides the interface.
// Actual communication with the front panel is handled via Wire (I2C master)
// in main.cpp. Key injection is done by driving GPIO pins.

HW650Emulator* HW650Emulator::_instance = nullptr;

static const uint8_t SEGMENT_MAP[] = {
    0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F
};

void HW650Emulator::begin(uint8_t pinDat, uint8_t pinClk) {
    _dat = pinDat;
    _clk = pinClk;
    memset((void*)_digits, 0, sizeof(_digits));
    _control = 0;
    _keyResponse = 0;
    _changed = false;
    _startDetected = false;
    _txCount = 0;
    _errCount = 0;
    _txLogIdx = 0;

    pinMode(_dat, INPUT_PULLUP);
    pinMode(_clk, INPUT_PULLUP);
    _instance = this;
}

bool HW650Emulator::poll() { return _changed; }

void IRAM_ATTR HW650Emulator::onSdaChange() {}
void IRAM_ATTR HW650Emulator::onClkChange() {}
void IRAM_ATTR HW650Emulator::onClkRising() {}
void IRAM_ATTR HW650Emulator::onClkFalling() {}
void HW650Emulator::handleTransaction() {}

uint8_t HW650Emulator::getDigit(uint8_t pos) const {
    if (pos > 3) return 0;
    return _digits[pos];
}
uint8_t HW650Emulator::getControl() const { return _control; }
void HW650Emulator::getDisplayText(char* buf) const {
    for (uint8_t i = 0; i < 4; i++) buf[i] = segmentsToChar(_digits[i]);
    buf[4] = '\0';
}
bool HW650Emulator::displayChanged() const { return _changed; }
void HW650Emulator::clearChanged() { _changed = false; }
void HW650Emulator::setKeyResponse(uint8_t keyCode) { _keyResponse = keyCode; }
void HW650Emulator::clearKeyResponse() { _keyResponse = 0; }
uint8_t HW650Emulator::getKeyResponse() const { return _keyResponse; }
void HW650Emulator::setAutoClear(bool on) { _autoClear = on; }

char HW650Emulator::segmentsToChar(uint8_t segments) {
    uint8_t seg = segments & 0x7F;
    if (seg == 0x00) return ' ';
    for (uint8_t i = 0; i < 10; i++) {
        if (SEGMENT_MAP[i] == seg) return '0' + i;
    }
    switch (seg) {
        case 0x40: return '-';  case 0x08: return '_';
        case 0x77: return 'A';  case 0x7C: return 'b';
        case 0x39: return 'C';  case 0x5E: return 'd';
        case 0x79: return 'E';  case 0x71: return 'F';
        case 0x76: return 'H';  case 0x38: return 'L';
        case 0x54: return 'n';  case 0x5C: return 'o';
        case 0x73: return 'P';  case 0x50: return 'r';
        case 0x78: return 't';  case 0x3E: return 'U';
        default: return '?';
    }
}

uint8_t HW650Emulator::charToSegments(char c) {
    if (c >= '0' && c <= '9') return SEGMENT_MAP[c - '0'];
    switch (c) {
        case '-': return 0x40;  case '_': return 0x08;  case ' ': return 0x00;
        case 'A': case 'a': return 0x77;  case 'b': case 'B': return 0x7C;
        case 'C':           return 0x39;  case 'c':           return 0x58;
        case 'd': case 'D': return 0x5E;  case 'E': case 'e': return 0x79;
        case 'F': case 'f': return 0x71;  case 'H': case 'h': return 0x76;
        case 'L': case 'l': return 0x38;  case 'n': case 'N': return 0x54;
        case 'o': case 'O': return 0x5C;  case 'P': case 'p': return 0x73;
        case 'r': case 'R': return 0x50;  case 't': case 'T': return 0x78;
        case 'u': case 'U': return 0x3E;  case 'y': case 'Y': return 0x6E;
        default: return 0x00;
    }
}

bool HW650Emulator::isOurAddress(uint8_t addr8bit) const {
    uint8_t base = addr8bit & 0xFE;
    switch (base) {
        case DISP_CTRL_ADDR:
        case KEY_ADDR_FD650 & 0xFE:
        case DISP_BASE_ADDR:
        case DISP_BASE_ADDR + 2:
        case DISP_BASE_ADDR + 4:
        case DISP_BASE_ADDR + 6:
            return true;
        default:
            return false;
    }
}

void IRAM_ATTR HW650Emulator::trace(uint8_t stage, uint8_t addr, uint8_t clk, uint8_t dat) {}
void IRAM_ATTR HW650Emulator::logTx(uint8_t addr, uint8_t data, bool isRead) {}

void HW650Emulator::printTrace(Print& out) const {
    out.printf("TX: %lu, Errors: %lu\n",
        (unsigned long)_txCount, (unsigned long)_errCount);
}

void HW650Emulator::printTxLog(Print& out) const {
    out.println(F("(Emulator stub — use front panel for display)"));
}

uint8_t HW650Emulator::slaveReceiveByte() { return 0; }
void HW650Emulator::slaveSendByte(uint8_t) {}
void HW650Emulator::slaveAck() {}
void HW650Emulator::slaveNack() {}
bool HW650Emulator::waitClkHigh(uint32_t) { return false; }
bool HW650Emulator::waitClkLow(uint32_t) { return false; }
void HW650Emulator::waitForStop(uint32_t) {}
