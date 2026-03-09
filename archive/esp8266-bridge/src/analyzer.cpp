#include "analyzer.h"

void SignalAnalyzer::begin(uint8_t pinDat, uint8_t pinClk) {
    _dat = pinDat;
    _clk = pinClk;
    _count = 0;
    pinMode(_dat, INPUT_PULLUP);
    pinMode(_clk, INPUT_PULLUP);
    _lastClk = digitalRead(_clk);
    _lastDat = digitalRead(_dat);
}

uint16_t SignalAnalyzer::captureEdges(uint32_t duration_ms) {
    _count = 0;
    uint32_t start = micros();
    uint32_t end = start + (duration_ms * 1000);
    uint32_t lastYield = millis();

    uint8_t prevClk = digitalRead(_clk);
    uint8_t prevDat = digitalRead(_dat);

    // Tight polling loop — ESP8266 at 80MHz gives ~100ns resolution
    while (micros() < end && _count < ANALYZER_BUF_SIZE) {
        uint8_t clk = digitalRead(_clk);
        uint8_t dat = digitalRead(_dat);

        if (clk != prevClk || dat != prevDat) {
            _buf[_count].timestamp_us = micros() - start;
            _buf[_count].clk = clk;
            _buf[_count].dat = dat;
            _count++;
            prevClk = clk;
            prevDat = dat;
        }

        // Feed watchdog periodically (no yield — preserves capture timing)
        if (millis() - lastYield > 500) {
            ESP.wdtFeed();
            lastYield = millis();
        }
    }

    return _count;
}

void SignalAnalyzer::printEdgeLog(Print &output) {
    output.println(F("=== Edge Log ==="));
    output.println(F("  Time(us)  CLK  DAT"));
    for (uint16_t i = 0; i < _count; i++) {
        char line[40];
        snprintf(line, sizeof(line), "  %8lu    %d    %d",
                 (unsigned long)_buf[i].timestamp_us,
                 _buf[i].clk, _buf[i].dat);
        output.println(line);
    }
    output.printf("=== %u events captured ===\n", _count);
}

void SignalAnalyzer::decodeI2C(Print &output) {
    output.println(F("=== I2C Decode ==="));

    // Walk through events looking for START/STOP/data
    enum State { IDLE, READING_BITS } state = IDLE;
    uint8_t currentByte = 0;
    uint8_t bitCount = 0;
    bool inFrame = false;

    for (uint16_t i = 1; i < _count; i++) {
        uint8_t prevClk = _buf[i-1].clk;
        uint8_t prevDat = _buf[i-1].dat;
        uint8_t clk = _buf[i].clk;
        uint8_t dat = _buf[i].dat;

        // START condition: DAT falls while CLK is high
        if (prevDat == 1 && dat == 0 && clk == 1 && prevClk == 1) {
            output.printf("[%lu us] START\n", (unsigned long)_buf[i].timestamp_us);
            currentByte = 0;
            bitCount = 0;
            inFrame = true;
            state = READING_BITS;
            continue;
        }

        // STOP condition: DAT rises while CLK is high
        if (prevDat == 0 && dat == 1 && clk == 1 && prevClk == 1) {
            output.printf("[%lu us] STOP\n", (unsigned long)_buf[i].timestamp_us);
            inFrame = false;
            state = IDLE;
            continue;
        }

        // Rising CLK edge = sample data bit
        if (prevClk == 0 && clk == 1 && inFrame) {
            if (bitCount < 8) {
                currentByte = (currentByte << 1) | dat;
                bitCount++;
                if (bitCount == 8) {
                    uint8_t addr7 = currentByte >> 1;
                    bool rw = currentByte & 1;
                    output.printf("[%lu us] BYTE: 0x%02X",
                                  (unsigned long)_buf[i].timestamp_us, currentByte);
                    if (state == READING_BITS && bitCount == 8) {
                        output.printf(" (addr=0x%02X %s)", addr7, rw ? "R" : "W");
                    }
                    output.println();
                }
            } else {
                // This is the ACK bit
                output.printf("[%lu us] %s\n",
                              (unsigned long)_buf[i].timestamp_us,
                              dat ? "NACK" : "ACK");
                currentByte = 0;
                bitCount = 0;
            }
        }
    }
    output.println(F("=== End Decode ==="));
}

uint32_t SignalAnalyzer::measureClockFreqHz(uint32_t sample_ms) {
    uint32_t edges = 0;
    uint32_t start = millis();
    uint32_t lastYield = start;
    uint8_t prev = digitalRead(_clk);

    while (millis() - start < sample_ms) {
        uint8_t cur = digitalRead(_clk);
        if (cur != prev) {
            edges++;
            prev = cur;
        }
        if (millis() - lastYield > 500) {
            ESP.wdtFeed();
            yield();
            lastYield = millis();
        }
    }

    // edges / 2 = full cycles, then scale to Hz
    return (edges * 500) / sample_ms;
}

void SignalAnalyzer::sniffAndPrint(Print &output) {
    uint8_t clk = digitalRead(_clk);
    uint8_t dat = digitalRead(_dat);

    if (clk != _lastClk || dat != _lastDat) {
        // Detect START/STOP
        if (_lastClk == 1 && clk == 1) {
            if (_lastDat == 1 && dat == 0) {
                output.print(F("[START] "));
            } else if (_lastDat == 0 && dat == 1) {
                output.println(F("[STOP]"));
            }
        }
        // Data bit on rising CLK
        if (_lastClk == 0 && clk == 1) {
            output.print(dat);
        }

        _lastClk = clk;
        _lastDat = dat;
    }
}
