# Stretch Goals

Prioritized backlog of enhancements beyond the core deliverable (remote power/channel control via web UI).

**Note:** The original ESP8266 I2C/HW650EP approach was abandoned in favor of ESP32-C3 with direct NEC IR. See `archive/esp8266-bridge/` for the legacy code.

## Core Deliverable (must-have tonight)
- [x] ~~ESP8266 impersonates HW650EP, captures display, injects keys~~ Replaced: ESP32-C3 sends NEC IR directly (demodulated line, no carrier)
- [x] Web UI: power on/off, channel up/down (full 40-button remote served from ESP32-C3)
- [x] WiFi connection to homelab

## Stretch Goals (in priority order)

### 1. MJPEG Live Stream from HDMI Capture — DONE
- [x] ~~Stream STB HDMI output to browser via MJPEG over HTTP~~ Upgraded to HLS (VAAPI H.264 + AAC) on .180
- [x] Use `/dev/video0` capture device
- [x] Lets user see STB output without physical HDMI splitter

### 2. WebSocket for Fast Button/Display Response — SKIPPED
- N/A — The I2C display capture concept was abandoned with the ESP8266 approach
- Current design uses HTTP fetch for IR commands (adequate latency for remote control)

### 3. Visual QA with Headless Browser — PARTIAL
- [x] Lightweight CSS theming verified to look good
- [x] No external CSS libraries (inline styles, system fonts)
- [ ] Automated screenshot comparison using headless Chrome/Puppeteer

### 4. End-to-End Verification via HDMI Capture — DONE
- [x] Press button in web UI → verify channel change via HDMI capture frame analysis
- [x] OCR or pixel comparison on captured frames (tools/ocr_sweep_monitor.py, ocr_monitor_logfile.py)
- [x] Automated regression test: inject key → wait → grab frame → verify (tools/sync_sweep_v2.py, retest_hits.py)

### 5. OTA Firmware Update — DONE
- [x] ArduinoOTA on ESP32-C3 with password authentication (env var `DMR39_OTA_PASS`)
- [x] **Safety:** unique hostname `dmr39-ir` via mDNS
- [x] **Security:** password-protected OTA (ArduinoOTA with `OTA_PASSWORD`)

### 6. E-Paper Status Updates — DONE
- [x] Post progress/completion to homelab e-paper board (192.168.50.72:8090)
- [x] Agent self-reports significant milestones

---

## Future Expansion

### 7. Live Subtitle OCR + Transcript Log
- Run OCR on the subtitle region of HLS frames (Tesseract/EasyOCR on .180 or .156)
- Detect channel switches via large frame diff → clear transcript
- Serve as a searchable web log of what was said on screen
- OCR tooling already exists in `tools/` from the IR sweep work

### 8. IR Receiver as Home Automation Trigger
- ESP32-C3 already captures and decodes any NEC remote pointed at it
- Map spare remote buttons to webhooks → trigger home automation actions (lights, fan, etc.)
- No app or smartphone needed — any old IR remote becomes a smart home controller

### 9. IR Learning Mode in Web UI
- Currently IR capture/labeling is only via serial CLI (`tools/ircli.py`)
- Add an HTTP endpoint on the ESP32-C3 to enter learn mode, capture a code, and assign a label
- Makes it self-service: point any remote, press button, name it in the browser
