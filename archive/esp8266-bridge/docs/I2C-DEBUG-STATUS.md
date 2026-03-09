# I2C Emulator Debug Status

## Current State (V8 - Polling)
- Polling-based approach (no interrupts)
- `digitalRead()` for all signal reading
- Direct register writes for SDA driving (ACK/data)
- Tight polling loop in `runNormalMode()` (5ms burst, then yield for WiFi)

## Problem
Reading wrong addresses: consistently `0x9F` and `0xFF` instead of expected `0x48`, `0x68`, `0x6A`, `0x6C`, `0x6E`, `0x4F`.

## Versions Tried

| Version | Strategy | Result |
|---------|----------|--------|
| V1-V3 | Various ISR approaches | Missed transactions or read 0xFF |
| V4 | Dual ISR (SDA+CLK) | **Best: 39 TX, read real addresses** but unstable |
| V5 | Single CLK CHANGE ISR | 100k+ interrupts/sec overwhelmed ESP |
| V6 | SDA FALLING ISR + GPI spin-wait | Read 0x9F/0xFF — GPI too fast, picks up noise |
| V7 | SDA FALLING ISR + digitalRead | Read 0xFF — ISR triggering on noise, not real START |
| V7b | ISR with double-check | 0 false starts, 0 real starts — confirmed ALL triggers were noise |
| V8 | Polling in loop() + digitalRead | Read 0x9F/0xFF — same wrong addresses |

## Key Observations

### Raw GPI capture after START detection
- CLK oscillates at ~250kHz (every 2μs) — this is NOISE, not real I2C
- SDA reads as 1 throughout — master data is invisible in the noise
- Pattern is identical regardless of timing (early or late capture)

### Pin diagnostic at boot (using digitalRead)
- GPIO4 (DAT): 12 edges/sec, idle=1
- GPIO5 (CLK): 24 edges/sec, idle=1
- Consistent across all boots, confirming correct pin assignment

### Transaction timing
- STB polls at ~5.5 Hz (180ms between transactions)
- Without ACK: transactions last ~15μs
- With ACK (from V4): full transaction ~200μs
- Bus clock within transactions: ~50kHz

### Address 0x9F analysis
- `0x9F = 10011111` binary
- Expected `0x48 = 01001000`
- NOT a simple bit inversion, shift, or endianness swap
- Appears consistently across ISR and polling approaches
- Suggests systematic timing error in bit sampling

## Hypotheses

1. **Signal integrity**: CLK ringing causes false edge detection, even with digitalRead
2. **Bit timing mismatch**: We may be sampling during the wrong CLK phase
3. **Bus loading**: ESP8266's INPUT_PULLUP (~50kΩ) may not be strong enough; the bus may need external pullups
4. **Protocol mismatch**: FD650/HW650 may use a slightly different timing than standard I2C

## What Worked (V4)
V4 used separate ISRs for SDA and CLK, processed bits on CLK rising edge, and read addresses correctly (0x48, 0x68, etc.). The issue was stability — false START/STOP detection corrupted the state machine.

## Next Steps
- Consider returning to V4 approach with improved state machine
- Add external 4.7kΩ pullup resistors to SDA and CLK for better signal quality
- Try hardware I2C (Wire library) in slave mode despite ESP8266 limitations
- Sniff the front panel board's I2C traffic for key code discovery (separate pins)
