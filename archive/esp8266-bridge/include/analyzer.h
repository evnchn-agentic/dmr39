#pragma once
#include <Arduino.h>
#include "config.h"

// ── Signal Analyzer ──
// Uses the ESP8266's speed to capture bus traffic on DAT/CLK lines.
// Useful when you have no oscilloscope/logic analyzer.
//
// Modes:
//   1. Edge logger — timestamps every CLK/DAT transition
//   2. I2C decoder — reconstructs bytes from clock/data
//   3. Frequency counter — measures CLK speed

#define ANALYZER_BUF_SIZE 2048

struct BusEvent {
    uint32_t timestamp_us;
    uint8_t  clk : 1;
    uint8_t  dat : 1;
};

class SignalAnalyzer {
public:
    void begin(uint8_t pinDat, uint8_t pinClk);

    // Capture raw transitions for duration_ms
    uint16_t captureEdges(uint32_t duration_ms);

    // Decode captured data as I2C-like frames
    void decodeI2C(Print &output);

    // Print raw edge log
    void printEdgeLog(Print &output);

    // Measure bus clock frequency
    uint32_t measureClockFreqHz(uint32_t sample_ms = 100);

    // Continuous sniffing mode (call in loop)
    void sniffAndPrint(Print &output);

    uint16_t eventCount() const { return _count; }

private:
    uint8_t _dat;
    uint8_t _clk;
    BusEvent _buf[ANALYZER_BUF_SIZE];
    uint16_t _count;
    uint8_t _lastClk;
    uint8_t _lastDat;
};
