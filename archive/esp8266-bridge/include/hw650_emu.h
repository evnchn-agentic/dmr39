#pragma once
#include <Arduino.h>
#include "config.h"

// ── HW650EP Emulator ──
// Impersonates the front panel chip to the STB motherboard.
//
// Architecture:
//   STB Motherboard (I2C master) ──DAT/CLK──> ESP8266 (I2C slave)
//   The front panel board is REMOVED. The ESP8266 pretends to be the HW650EP.
//
// What the STB does on the bus:
//   WRITE 0x48 <ctrl>         → display control (on/off, brightness)
//   WRITE 0x68 <seg>          → digit 1 segment data
//   WRITE 0x6A <seg>          → digit 2 segment data
//   WRITE 0x6C <seg>          → digit 3 segment data
//   WRITE 0x6E <seg>          → digit 4 segment data
//   READ  0x49 or 0x4F        → key scan (we respond with injected key)
//
// What the ESP does:
//   - Captures display writes → we know what channel is showing
//   - Responds to key reads   → we inject CH+/CH-/Standby
//
// I2C slave is interrupt-driven: SDA falling edge triggers START detection ISR.

class HW650Emulator {
public:
    void begin(uint8_t pinDat, uint8_t pinClk);

    // Call frequently from loop() to service bus traffic.
    // Returns true if new display data was received this call.
    bool poll();

    // ── Display state (received from STB master) ──
    uint8_t getDigit(uint8_t pos) const;
    uint8_t getControl() const;
    void getDisplayText(char* buf) const;
    bool displayChanged() const;
    void clearChanged();

    // ── Key injection (sent to STB when it polls for key presses) ──
    void setKeyResponse(uint8_t keyCode);
    void clearKeyResponse();
    uint8_t getKeyResponse() const;

    // Auto-clear: after the STB reads the key, clear it automatically
    void setAutoClear(bool on);

    // ── Segment decode utilities ──
    static char segmentsToChar(uint8_t segments);
    static uint8_t charToSegments(char c);

    // Stats
    uint32_t getTxCount() const { return _txCount; }
    uint32_t getErrorCount() const { return _errCount; }

private:
    uint8_t _dat, _clk;

    // Display state captured from STB writes
    volatile uint8_t _digits[4] = {0};
    volatile uint8_t _control = 0;
    volatile bool _changed = false;

    // Key response to send when STB reads key register
    volatile uint8_t _keyResponse = 0;
    bool _autoClear = true;

    // Dual-ISR approach: SDA change for START/STOP, CLK change for bit processing
    static void IRAM_ATTR onSdaChange();
    static void IRAM_ATTR onClkChange();
    static void IRAM_ATTR onClkRising();
    static void IRAM_ATTR onClkFalling();
    void IRAM_ATTR handleTransaction();
    static HW650Emulator* _instance;
    volatile bool _startDetected = false;

    // Stats
    volatile uint32_t _txCount = 0;
    volatile uint32_t _errCount = 0;
    volatile uint32_t _isrCount = 0;       // total ISR entries
    volatile uint32_t _isrSdaHigh = 0;     // rejected: SDA was high (not falling)
    volatile uint32_t _isrClkLow = 0;      // rejected: CLK was low (data change, not START)
    volatile uint32_t _nackCount = 0;      // address didn't match
    volatile uint8_t _lastNackAddr = 0;    // last NACK'd address
    // Ring buffer of last 8 addresses seen (for debugging)
    volatile uint8_t _addrLog[8] = {0};
    volatile uint8_t _addrLogIdx = 0;
    // Raw bit timing log for one transaction
    struct BitSample {
        uint16_t us;     // microseconds since START
        uint8_t clk;
        uint8_t sda;
        uint8_t event;   // 0=waitHigh, 1=sample, 2=waitLow
    };
    volatile BitSample _bitLog[20];
    volatile uint8_t _bitLogIdx = 0;
    volatile bool _bitLogDone = false;
    // Raw GPI register capture with timestamps
    struct RawSample {
        uint32_t gpi;
        uint32_t us;
    };
    volatile RawSample _rawCapture[200];
    volatile uint16_t _rawCaptureCount = 0;

    // Transaction log — ring buffer of completed transactions
    static const uint8_t TRACE_SIZE = 32;
    static const uint8_t TX_LOG_SIZE = 64;
    struct TraceEntry {
        uint32_t us;
        uint8_t stage;
        uint8_t addr;
        uint8_t clk;
        uint8_t dat;
    };
    struct TxLogEntry {
        uint8_t addr;     // 8-bit address (includes R/W)
        uint8_t data;     // data byte (sent or received)
        uint8_t isRead;   // 1=read, 0=write
    };
    volatile TraceEntry _trace[32];
    volatile uint8_t _traceIdx = 0;
    volatile TxLogEntry _txLog[64];
    volatile uint8_t _txLogIdx = 0;
    void IRAM_ATTR trace(uint8_t stage, uint8_t addr, uint8_t clk, uint8_t dat);
    void IRAM_ATTR logTx(uint8_t addr, uint8_t data, bool isRead);
public:
    void printTrace(Print& out) const;
    void printTxLog(Print& out) const;

    // ── I2C slave helpers ──
    bool isOurAddress(uint8_t addr8bit) const;
    uint8_t slaveReceiveByte();
    void slaveSendByte(uint8_t data);
    void slaveAck();
    void slaveNack();
    bool waitClkHigh(uint32_t timeout_us = 500);
    bool waitClkLow(uint32_t timeout_us = 500);
    void waitForStop(uint32_t timeout_us = 2000);
};
