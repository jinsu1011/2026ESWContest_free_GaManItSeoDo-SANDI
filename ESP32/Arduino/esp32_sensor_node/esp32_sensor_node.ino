/*
 * SafeNest ESP32 sensor node
 *
 * Sensors:
 *   - Seeed MR60BHA2 (UART2 GPIO 16/17): respiration and heart rate
 *   - Winsen MH-Z19B (UART1 GPIO 32/33): CO2
 *   - PIR (digital input): motion
 *   - Waveshare Thermal Camera HAT / MI48xx (I2C control + SPI data): 80 x 62
 *
 * Transport:
 *   - TCP for low-rate mmWave/CO2/PIR JSON telemetry
 *   - Chunked UDP for big-endian uint16 Thermal frames
 *
 * The Arduino loop never calls delay(). Sensor scheduling uses millis().
 * TCP reconnect/writes and Thermal UDP writes run in separate FreeRTOS tasks.
 * If the network is slow, the one-slot thermal queue keeps only the newest frame.
 *
 * MH-Z19B power: Vin 4.5-5.5 V, peak 150 mA. Do not power from the ESP32 3.3 V
 * rail. UART is 9600 8N1 TTL 3.3 V with TX/RX crossed on UART1 (GPIO 32/33).
 * Never share UART2 (GPIO 16/17) with the MR60.
 */

#include <Arduino.h>
#include <esp_system.h>
#include <freertos/event_groups.h>
#include <freertos/semphr.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <SPI.h>
#include <errno.h>
#include <lwip/sockets.h>
#include "Seeed_Arduino_mmWave.h"
#include "secrets.h"

// -----------------------------------------------------------------------------
// Device identity. Wi-Fi and Raspberry Pi settings live in ignored secrets.h.
// ----------------------------------------------------------------------------
constexpr char DEVICE_ID[] = "esp32-01";
char bootId[33] = {};
constexpr uint16_t THERMAL_UDP_PORT = 5005;

// -----------------------------------------------------------------------------
// ESP32 Dev Module wiring. MH-Z19B is on UART1; MR60 stays on UART2.
// ----------------------------------------------------------------------------
constexpr int PIN_I2C_SDA = 21;
constexpr int PIN_I2C_SCL = 22;
constexpr int PIN_PIR = 13;
constexpr int PIN_MMWAVE_RX = 16;  // ESP32 RX <- MR60BHA2 TX
constexpr int PIN_MMWAVE_TX = 17;  // ESP32 TX -> MR60BHA2 RX
// UART1 for MH-Z19B. Never share UART2 (16/17) with the MR60.
// GPIO 32/33 are free after the v2 pin map (I2C 21/22, PIR 13, thermal
// SPI/I2C 18/19/23/27/26/25). They are not flash/strapping pins on WROOM.
constexpr int PIN_MHZ19_RX = 32;  // ESP32 RX <- MH-Z19B TX
constexpr int PIN_MHZ19_TX = 33;  // ESP32 TX -> MH-Z19B RX

constexpr int PIN_THERMAL_SCLK = 18;
constexpr int PIN_THERMAL_MISO = 19;
constexpr int PIN_THERMAL_MOSI = 23;
constexpr int PIN_THERMAL_CS = 27;
constexpr int PIN_THERMAL_READY = 26;
constexpr int PIN_THERMAL_RESET = 25;

constexpr uint32_t USB_BAUD = 115200;
constexpr uint32_t MMWAVE_BAUD = 115200;
constexpr uint32_t MHZ19_BAUD = 9600;
constexpr uint32_t THERMAL_SPI_HZ = 8000000;

// Runtime schedules.
// MH-Z19B command 0x86 has no Sensirion-style getDataReadyStatus. Winsen
// manuals (v1.7, 2020-10-15) do not prove each UART read is a new NDIR
// conversion. This node therefore infers producer identity:
//   - at most one accepted UART sample per CO2_UART_SAMPLE_PERIOD_MS
//   - event identity class INFERRED_UART_SAMPLE (not SCD40 conversion)
// 5000 ms matches the SCD4x ~5 s cadence this v2 node used to expose and
// is slower than the ~1 s PWM encoding period. It is NOT a claim that the
// optical cell converts at this rate.
constexpr uint32_t PIR_PERIOD_MS = 20;
constexpr uint32_t CO2_UART_SAMPLE_PERIOD_MS = 5000;
constexpr uint32_t CO2_UART_RESPONSE_TIMEOUT_MS = 200;
constexpr uint32_t CO2_PREHEAT_MS = 180000;  // Winsen: 3 min
constexpr uint16_t CO2_PPM_MAX_ACCEPTED = 10000;
// 10 Hz matches the 1.3.0 phase contract. PHASE_MAX_AGE_MS is 500, so a 1 Hz
// snapshot would publish breath_phase as null on almost every packet and the
// Pi 300-sample window would never fill. LCD can ignore the extra packets.
// TCP writes remain isolated in their own task.
constexpr uint32_t TELEMETRY_PERIOD_MS = 100;
constexpr uint32_t HEALTH_LOG_PERIOD_MS = 10000;
// If MI48 is unpowered or hung off I2C, a probe-first init never pulses
// GPIO25. Recovery always resets then probes. Serial 't' is debug-only.
constexpr uint32_t THERMAL_RECOVER_PERIOD_MS = 2000;
constexpr uint32_t THERMAL_STALE_REINIT_MS = 7000;
constexpr uint32_t THERMAL_I2C_MISS_LOG_MS = 10000;
constexpr uint32_t MMWAVE_STALE_MS = 10000;
constexpr uint32_t CO2_STALE_MS = 15000;
constexpr uint32_t PHASE_MAX_AGE_MS = 500;
// The MR60 reports 0x0F09 occupancy on its own cadence, independent of the
// respiration/heart-rate stream. This bound only has to outlive normal report
// gaps; once it lapses the field goes null (unknown), which suppresses mmWave
// inference rather than asserting an empty room.
constexpr uint32_t PRESENCE_MAX_AGE_MS = 5000;
constexpr char NODE_FIRMWARE_VERSION[] =
    "safenest-esp32-sensor-node/1.7.10-mhz19b.1";
constexpr char DIAGNOSTIC_BUILD_ID[] =
    "mhz19b-thermal-reinit-20260901-09";
constexpr char MMWAVE_SCHEMA_VERSION[] = "1.3";
constexpr char CO2_SENSOR_MODEL[] = "MH-Z19B";
constexpr char CO2_EVENT_IDENTITY_CLASS[] = "INFERRED_UART_SAMPLE";

// -----------------------------------------------------------------------------
// Link diagnostics.
//
// A "disconnect" on this link is really a missed receive deadline on the Pi.
// Both receivers close the socket themselves when one packet does not arrive
// in time, and neither waits as long as this node is willing to stall:
//
//   RaspberryPi/LCD/server.py             connection.settimeout(2.0). Its
//                                         header recv is wrapped in
//                                         `except socket.timeout: continue`,
//                                         but the payload recv is not, so one
//                                         2 s gap between the 16-byte header
//                                         and the JSON body raises
//                                         socket.timeout -- an OSError, which
//                                         its handler treats as fatal.
//   RaspberryPi/Runtime/gateway/          5 s total deadline per field.
//     receiver.py
//
// Arduino-ESP32 3.x NetworkClient::write() is not bounded by writeAll()'s
// old 5000 ms stall timer. It select()-waits WIFI_CLIENT_SELECT_TIMEOUT_US
// (1 s) up to WIFI_CLIENT_MAX_WRITE_RETRY (10) times, so one client.write()
// can sit in the TX mutex for ~10000 ms. Field logs of write_max_ms=10012
// with stall_ms=0/zero_writes=0 are that path: the wait happens inside
// write(), so the application timer never starts. The Pi LCD receiver has
// already closed (2 s) long before that returns, which is why peer_closed,
// tcp_errno=104 (ECONNRESET) and connect_ms≈1500 follow.
//
// writeAll() therefore calls lwip_send(MSG_DONTWAIT) itself. An 800 ms retry
// loop on EAGAIN was still long enough to desynchronize the Pi: a partial
// SNST prefix (field wrote=97/808) forces this node to close, then
// reconnect SYN storms fill the TX queue even with thermal UDP off. Skip
// before any byte is on the wire; only a started packet uses the finish
// deadline.
// ----------------------------------------------------------------------------
constexpr uint32_t PI_PACKET_DEADLINE_MS = 2000;
// Warn below the Pi deadline so the log shows the approach, not only the crash.
constexpr uint32_t TELEMETRY_GAP_WARN_MS = 1500;
constexpr uint32_t TCP_WRITE_WARN_MS = 150;
// No byte sent yet: abandon this snapshot and keep the socket.
constexpr uint32_t TCP_WRITE_SKIP_MS = 40;
// Some bytes already on the wire: finish or close (partial SNST is fatal).
constexpr uint32_t TCP_WRITE_DEADLINE_MS = 200;
constexpr uint32_t TCP_CONNECT_TIMEOUT_MS = 4000;
// After a partial-write close, wait before SYN. 300 ms was still fast enough
// to pile SYN-RECV on a weak AP uplink (errno 119 / connect_ms=1503).
constexpr uint32_t TCP_RECONNECT_DELAY_MS = 2000;
constexpr uint32_t TCP_CONNECT_FAIL_DELAY_MS = 2500;
constexpr uint8_t TCP_CONNECT_FAILS_BEFORE_REASSOC = 5;
constexpr uint32_t SERIAL_ERROR_LOG_PERIOD_MS = 1000;
constexpr uint32_t TCP_MUTEX_WARN_MS = 100;
// UDP sendto is O_NONBLOCK, so the mutex should be held for milliseconds.
// 250 ms turns a stuck datagram into a counted timeout instead of a 3 s
// invisible wait that itself misses the Pi deadline.
constexpr uint32_t TCP_MUTEX_MAX_WAIT_MS = 250;
constexpr uint32_t UDP_DATAGRAM_WARN_MS = 40;
// 2 ms between chunks was only a CPU yield. Nine 1200-byte datagrams still
// hit the radio in ~34 ms, which is what filled the lwIP pool (errno 12) and
// left no airtime for the 1 Hz TCP write. 20 ms is long enough for the TCP
// task to wake, take the mutex, and finish a ~3 ms write, and 8 gaps stay
// inside the ~1000 ms 1 fps period and the Pi's 500 ms thermal reassembly
// window (measured from the last chunk, not the first).
constexpr uint32_t UDP_CHUNK_GAP_MS = 20;
// A live telemetry write holds TCP_CRITICAL_BIT for a few milliseconds.
// Waiting this out and then continuing the same frame is the interleave;
// returning Preempted used to drop the remaining chunks. Connect() holds the
// bit much longer, but tcpLinkHealthy is already false then, so we stand down
// instead of waiting out the handshake.
constexpr uint32_t UDP_YIELD_TO_TCP_MAX_MS = 80;
// Free-heap low-water used to trigger udp.stop()/begin() every 5 s. That
// malloc/free of the 1460-byte WiFiUDP TX buffer plus a new PCB is what
// produced heap≈74 KB with max_alloc≈45 KB (fragmentation, not a leak):
// stopping TX recovered 74→90→105→122 KB as lwIP pbufs drained. ENOMEM
// now pauses the thermal sender so those pbufs can return; the socket is
// rebuilt only when the fd itself looks dead.
constexpr uint32_t HEAP_RECLAIM_LOW_WATER_BYTES = 48000;
constexpr uint32_t UDP_ENOMEM_BACKOFF_MIN_MS = 250;
constexpr uint32_t UDP_ENOMEM_BACKOFF_MAX_MS = 2000;
constexpr uint32_t UDP_SLOW_TCP_HOLDOFF_MS = 250;
// udpStarted was only cleared when Wi-Fi dropped, so once sendto() entered a
// failing state the task retried the same socket forever: udp_sent froze at 61
// for the rest of the run while udp_failed kept climbing. Rebuilding the socket
// after a run of failures gives the transmitter a way back without a reboot.
constexpr uint32_t UDP_MAX_CONSECUTIVE_FAILURES = 10;
// The per-2-second [link] firehose was 38 fields on one line, five times more
// often than [health]. Same cadence as [health] keeps it readable; 's' still
// prints one on demand and 'l' silences the periodic copy.
constexpr uint32_t DIAG_LOG_PERIOD_MS = 10000;

enum class Mhz19Txn : uint8_t {
  IDLE,
  AWAIT_RESPONSE,
};

// -----------------------------------------------------------------------------
// Thermal-camera constants (MI48xx + MI0801/MI0802, 80 x 62).
// ----------------------------------------------------------------------------
constexpr uint8_t THERMAL_ADDRESS_A = 0x40;
constexpr uint8_t THERMAL_ADDRESS_B = 0x41;

// Winsen MH-Z19B UART read-concentration command 0x86 (9 bytes, 9600 8N1).
// checksum = (NOT (byte1..byte7)) + 1; ppm = high*256 + low.
constexpr uint8_t MHZ19_READ_CMD[9] = {0xFF, 0x01, 0x86, 0x00, 0x00,
                                       0x00, 0x00, 0x00, 0x79};

constexpr uint8_t REG_EVK_TEST = 0x00;
constexpr uint8_t REG_SENSOR_POWERUP = 0xB0;
constexpr uint8_t REG_FRAME_MODE = 0xB1;
constexpr uint8_t REG_FW_VERSION_1 = 0xB2;
constexpr uint8_t REG_FW_VERSION_2 = 0xB3;
constexpr uint8_t REG_FRAME_RATE = 0xB4;
constexpr uint8_t REG_STATUS = 0xB6;
constexpr uint8_t REG_SENSOR_TYPE = 0xBA;

constexpr uint8_t STATUS_DATA_READY = 0x10;
constexpr uint8_t STATUS_BOOTING = 0x20;
constexpr uint8_t MODE_CONTINUOUS = 0x02;

constexpr size_t THERMAL_WIDTH = 80;
constexpr size_t THERMAL_HEIGHT = 62;
constexpr size_t THERMAL_PIXEL_COUNT = THERMAL_WIDTH * THERMAL_HEIGHT;
constexpr size_t THERMAL_HEADER_WORDS = THERMAL_WIDTH;
constexpr size_t THERMAL_CAPTURE_WORDS =
    THERMAL_HEADER_WORDS + THERMAL_PIXEL_COUNT;

// MI48 FRAME_RATE is a divisor of the ~25 FPS native sensor. 25 → ~1.0 FPS.
// Lowering this value raises bandwidth and ESP32 CPU/SPI load.
//
// Divisor 4 (6.25 FPS) is what the Wi-Fi TX path could not sustain: one frame
// is 9 datagrams, so 6.25 FPS put ~57 datagrams/s and ~64 KB/s on the radio in
// 160 ms bursts. Divisor 12 (~2.08 FPS) still competed with 1 Hz TCP until
// writes were fail-fast. 25 keeps the frame format identical at about half
// that UDP rate; raise it back only with the [link] udp_kbps and tcp drop
// counters in view.
constexpr uint8_t THERMAL_FRAME_RATE_DIVIDER = 25;

// -----------------------------------------------------------------------------
// SafeNest TCP protocol v1 for scalar telemetry.
// Outer header (16 bytes, network byte order):
//   magic[4]="SNST", version:u8, type:u8, flags:u16,
//   packet_sequence:u32, payload_length:u32
// Packet type 1 payload: UTF-8 JSON.
// Thermal UDP logical payload: 16-byte metadata followed by 4960 uint16 words.
// -----------------------------------------------------------------------------
constexpr uint8_t PROTOCOL_VERSION = 1;
constexpr uint8_t PACKET_TELEMETRY_JSON = 1;
constexpr uint8_t PACKET_THERMAL_U16_BE = 2;
constexpr size_t PACKET_HEADER_SIZE = 16;
constexpr size_t THERMAL_META_SIZE = 16;

// SafeNest Thermal UDP v1. A 1200-byte datagram stays below the common
// Ethernet/Wi-Fi MTU after IPv4 and UDP headers, avoiding IP fragmentation.
// Header fields are network byte order and every chunk repeats the frame CRC32.
constexpr char THERMAL_UDP_MAGIC[] = "SNTU";
constexpr uint8_t THERMAL_UDP_VERSION = 1;
constexpr size_t THERMAL_UDP_HEADER_SIZE = 32;
constexpr size_t THERMAL_UDP_DATAGRAM_SIZE = 1200;
constexpr size_t THERMAL_UDP_CHUNK_SIZE =
    THERMAL_UDP_DATAGRAM_SIZE - THERMAL_UDP_HEADER_SIZE;
constexpr size_t THERMAL_PAYLOAD_SIZE =
    THERMAL_META_SIZE + THERMAL_PIXEL_COUNT * sizeof(uint16_t);
constexpr uint16_t THERMAL_UDP_CHUNK_COUNT =
    (THERMAL_PAYLOAD_SIZE + THERMAL_UDP_CHUNK_SIZE - 1) /
    THERMAL_UDP_CHUNK_SIZE;

struct TelemetrySnapshot {
  uint32_t sequence;
  uint32_t uptimeMs;
  float respirationRate;
  float heartRate;
  uint16_t co2Ppm;
  bool pirMotion;
  bool respirationValid;
  bool heartValid;
  bool co2Valid;
  bool phaseSamplePresent;
  // Inferred MH-Z19B UART sample identity. Same event_id on later TCP
  // snapshots is a cached retransmission; Pi slope must ignore repeats.
  uint32_t co2MeasurementEventId;
  uint32_t co2MeasurementMonotonicMs;
  bool co2MeasurementEventValid;
  bool co2Preheat;
  // Tri-state MR60 occupancy. `humanDetectedKnown == false` must serialize as
  // JSON null, never false: the Pi presence gate treats false as "room empty"
  // and would then suppress mmWave inference for the wrong reason.
  bool humanDetectedRaw;
  bool humanDetectedKnown;
  float totalPhase;
  float breathPhase;
  float heartPhase;
  uint32_t phaseTimestampMs;
  uint32_t phaseSequence;
};

struct ThermalTxFrame {
  uint32_t frameSequence;
  uint32_t uptimeMs;
  uint16_t minimumRaw;
  uint16_t maximumRaw;
  uint16_t pixels[THERMAL_PIXEL_COUNT];
};

// Per-write forensics. A bare bool made a 2 ms success and a 4.9 s stall that
// eventually succeeded look identical, yet only the second one outlives the
// Pi's receive timeout and gets the socket closed underneath us.
struct TcpWriteReport {
  size_t bytesWritten;
  uint32_t elapsedMs;
  uint32_t longestStallMs;
  uint16_t zeroWrites;
  uint16_t partialWrites;
  int lastErrno;
  bool connectedAtEnd;
};

enum class ThermalSendResult {
  Sent,
  Preempted,
  Failed,
  // The Serial kill switch dropped the frame. Kept distinct from Preempted so
  // a deliberate A/B test never inflates the contention counter.
  Suppressed,
  // The TCP session is down, so the transmitter yielded the radio to let it
  // reconnect. Distinct from Suppressed because nobody asked for it, and from
  // Preempted because no TCP write was actually waiting on the mutex.
  Deferred,
};

HardwareSerial mmWaveSerial(2);
HardwareSerial mhz19Serial(1);

// The library's own SEEED_MR60BHA2::isHumanDetected() cannot express "unknown":
// it returns false both when the room is empty and when no 0x0F09 report has
// been parsed yet, and it self-clears its validity flag on read. Overriding
// handleType() captures the report itself, so an absent report stays
// distinguishable from a negative one. The base implementation is still
// invoked, leaving library state untouched, and the base class has already
// verified both frame checksums before dispatching here.
class SafeNestMR60BHA2 : public SEEED_MR60BHA2 {
 public:
  bool handleType(uint16_t type, const uint8_t *data,
                  size_t dataLength) override {
    if (type == static_cast<uint16_t>(
                    TypeHeartBreath::ReportHumanDetection)) {
      // The vendor handler reads data[0] unguarded; refuse a truncated report
      // here instead of letting it read out of bounds.
      if (dataLength < 1) return false;
      presenceRaw_ = data[0] != 0;
      presencePending_ = true;
    }
    return SEEED_MR60BHA2::handleType(type, data, dataLength);
  }

  // Same one-shot out-parameter idiom as getBreathRate()/getHeartRate(): the
  // return value means "a new report was parsed", never "nobody is present".
  bool takePresence(bool &value) {
    if (!presencePending_) return false;
    presencePending_ = false;
    value = presenceRaw_;
    return true;
  }

  // SeeedmmWave::fetch() keeps a static startFrame that never times out.
  // One false SOF 0x01 consumes real frames as payload for the rest of the
  // boot. Assemble UART locally, then dispatch checksummed frames here.
  bool ingestFrame(const uint8_t *bytes, size_t len) {
    return processFrame(bytes, len);
  }

 private:
  bool presenceRaw_ = false;
  bool presencePending_ = false;
};

SafeNestMR60BHA2 mmWave;
SPIClass thermalSpi(VSPI);

QueueHandle_t telemetryQueue = nullptr;
QueueHandle_t thermalQueue = nullptr;
EventGroupHandle_t networkEvents = nullptr;
SemaphoreHandle_t networkTxMutex = nullptr;
constexpr EventBits_t TCP_CRITICAL_BIT = BIT0;

uint16_t thermalCapture[THERMAL_CAPTURE_WORDS];
// These ~10 KiB objects are global on purpose. Keeping either on a task stack
// can overflow the default Arduino loop stack on a non-PSRAM ESP32.
ThermalTxFrame thermalProducerFrame;
ThermalTxFrame thermalNetworkFrame;
uint8_t thermalAddress = 0;
bool thermalStarted = false;
uint32_t lastThermalRecoverMs = 0;
uint32_t lastThermalProgressMs = 0;
uint32_t lastRecoverCaptureAttempts = 0;

float respirationRate = NAN;
float heartRate = NAN;
float totalPhase = NAN;
float breathPhase = NAN;
float heartPhase = NAN;
uint16_t co2Ppm = 0;
bool pirMotion = false;
bool humanDetectedRaw = false;
bool phaseSamplePresent = false;

uint32_t lastRespirationMs = 0;
uint32_t lastHeartMs = 0;
uint32_t lastPresenceMs = 0;
uint32_t lastCo2Ms = 0;
uint32_t lastThermalStatusPollMs = 0;
uint32_t lastPirPollMs = 0;
uint32_t lastCo2PollMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t lastHealthLogMs = 0;
uint32_t telemetrySequence = 0;
uint32_t thermalSequence = 0;
uint32_t thermalCrcErrors = 0;
uint32_t thermalRangeErrors = 0;
uint32_t co2MeasurementEventId = 0;
uint32_t co2MeasurementMonotonicMs = 0;
bool co2MeasurementEventValid = false;
Mhz19Txn mhz19Txn = Mhz19Txn::IDLE;
uint32_t mhz19RequestSentMs = 0;
uint32_t co2ChecksumFailures = 0;
uint32_t co2TimeoutFailures = 0;
uint32_t co2ShortFrameFailures = 0;
uint32_t co2AcceptedSamples = 0;
uint16_t co2LastUartPpm = 0;
// Written by the network tasks on core 0 and read by loop() on core 1. Each
// counter has exactly one writer and aligned 32-bit accesses are atomic on
// Xtensa, so volatile only has to stop the compiler caching them in registers.
volatile uint32_t telemetryQueueOverwrites = 0;
volatile uint32_t thermalQueueOverwrites = 0;
volatile uint32_t tcpConnectionFailures = 0;
volatile uint32_t tcpSendFailures = 0;
volatile uint32_t thermalUdpFramesSent = 0;
volatile uint32_t thermalFramesPreempted = 0;
volatile uint32_t thermalTcpYields = 0;
volatile uint32_t thermalUdpSendFailures = 0;
volatile uint32_t heapReclaims = 0;
volatile uint32_t heapReclaimSkippedTcp = 0;
volatile uint32_t udpHoldoffEvents = 0;
volatile uint32_t udpHoldoffUntilMs = 0;
volatile uint32_t udpBackoffMs = 0;
volatile uint32_t tcpWriteTimeouts = 0;

// Link diagnostics written by the network tasks on core 0, under the same
// single-writer, aligned-32-bit rule as the counters above.
volatile uint32_t tcpSessions = 0;
volatile uint32_t tcpDrops = 0;
volatile uint32_t tcpShortSessions = 0;
volatile uint32_t tcpPeerClosed = 0;
volatile uint32_t tcpWriteStalls = 0;
volatile uint32_t tcpPartialWrites = 0;
volatile uint32_t tcpMutexTimeouts = 0;
volatile uint32_t tcpMutexWaitMaxMs = 0;
volatile uint32_t tcpMutexWaitSlow = 0;
volatile uint32_t tcpWriteMaxMs = 0;
volatile uint32_t tcpSlowWrites = 0;
volatile uint32_t telemetryGapMaxMs = 0;
volatile uint32_t telemetryGapOverWarn = 0;
volatile uint32_t telemetryGapOverDeadline = 0;
volatile uint32_t telemetryPacketsSent = 0;
volatile uint32_t lastTelemetrySentMs = 0;
volatile uint32_t udpDatagramsSent = 0;
volatile uint32_t udpBytesSent = 0;
volatile uint32_t udpDatagramMaxMs = 0;
volatile uint32_t udpSlowDatagrams = 0;
volatile uint32_t udpFrameMaxMs = 0;
volatile uint32_t thermalFramesSuppressed = 0;
volatile uint32_t thermalFramesDeferred = 0;
volatile uint32_t udpSocketRestarts = 0;
volatile int32_t tcpLastErrno = 0;
volatile int32_t udpLastErrno = 0;

// Set by the TCP task around a live session and read by the thermal task.
// Single writer, aligned 32-bit access, same rule as the counters above.
//
// This is the back-pressure signal. TCP telemetry is the contract with the Pi;
// thermal is best-effort. While the session is down the transmit path is either
// saturated or the link is bad, and a continuous thermal stream is precisely
// what keeps a SYN from completing -- so the thermal task stands down until the
// session is back rather than competing with the reconnect it is blocking.
volatile bool tcpLinkHealthy = false;

// Runtime A/B switches driven from the Serial console. Toggling the thermal
// transmitter without reflashing is the shortest path to proving whether the
// UDP stream is what breaks the TCP stream.
// Default on: field runs need thermal without a serial 'u'/'c' after every
// reset. Press 'u' or 'c' to isolate UDP vs SPI if TCP starts slipping.
volatile bool thermalUdpEnabled = true;
volatile bool thermalCaptureEnabled = true;

// Resolved once so thermal datagrams do not call gethostbyname() under the
// TX mutex. beginPacket(const char*) does that on every chunk.
IPAddress rpiHostIp;
bool rpiHostIpValid = false;

// Written and read only by loop() on core 1, so volatile is not warranted.
uint32_t thermalCaptureAttempts = 0;
uint32_t thermalReadyByPin = 0;
uint32_t thermalReadyByI2c = 0;
uint32_t thermalCaptureMaxMs = 0;
uint32_t loopIterations = 0;
uint32_t lastLinkLogMs = 0;
// [link] is a 38-field diagnostic line. Useful during a bisect, unreadable as a
// permanent fixture, so the periodic copy can be silenced ('l') without losing
// the on-demand one ('s').
bool linkDiagnosticsEnabled = true;
bool linkDiagnosticsOnce = false;
uint32_t thermalStatusQueryFailures = 0;
uint32_t mmWaveUpdateSuccesses = 0;
constexpr uint8_t MMWAVE_SOF_BYTE = 0x01;
constexpr size_t MMWAVE_FRAME_HEADER_SIZE = 8;
constexpr size_t MMWAVE_MAX_DATA_SIZE = 512;
constexpr uint32_t MMWAVE_FRAME_STALE_MS = 100;
constexpr uint8_t MMWAVE_HEXDUMP_BYTES = 24;
uint8_t mmWaveFrameBuf[MMWAVE_FRAME_HEADER_SIZE + MMWAVE_MAX_DATA_SIZE + 1];
size_t mmWaveFrameLen = 0;
bool mmWaveInFrame = false;
uint32_t mmWaveFrameStartedMs = 0;
bool mmWaveHexDumped = false;
uint32_t mmWaveUpdateMisses = 0;
uint32_t lastMmWaveUpdateMs = 0;
uint32_t lastPhaseMs = 0;
uint32_t phaseSequence = 0;
uint8_t thermalUdpDatagram[THERMAL_UDP_DATAGRAM_SIZE];

// Wrap-safe periodic scheduling helper. Updating by period, rather than assigning
// now, avoids gradual drift. A long overrun is collapsed to one execution.
bool scheduleDue(uint32_t now, uint32_t &lastRun, uint32_t period) {
  if (static_cast<uint32_t>(now - lastRun) < period) return false;
  lastRun += period;
  if (static_cast<uint32_t>(now - lastRun) >= period) lastRun = now;
  return true;
}

// TCP-task error lines are hundreds of characters. Unthrottled they block
// loop() on the Serial mutex long enough for MR60 UART2 to lose SOF sync,
// after which [health] prints resp=null even though the radar is still up.
bool tcpErrorLogDue() {
  static uint32_t lastMs = 0;
  const uint32_t now = millis();
  if (lastMs != 0 &&
      static_cast<uint32_t>(now - lastMs) < SERIAL_ERROR_LOG_PERIOD_MS) {
    return false;
  }
  lastMs = now;
  return true;
}

bool isFresh(uint32_t timestamp, uint32_t now, uint32_t timeout) {
  if (timestamp == 0) return false;
  const int32_t age = static_cast<int32_t>(now - timestamp);
  return age < static_cast<int32_t>(timeout);
}

bool deadlineReached(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

bool co2InPreheat(uint32_t now) {
  return now < CO2_PREHEAT_MS;
}

bool udpHoldoffActive(uint32_t now) {
  return udpHoldoffUntilMs != 0 &&
         static_cast<int32_t>(udpHoldoffUntilMs - now) > 0;
}

void requestUdpHoldoff(uint32_t now, uint32_t durationMs) {
  const uint32_t until = now + durationMs;
  const bool wasActive = udpHoldoffActive(now);
  if (!wasActive || static_cast<int32_t>(until - udpHoldoffUntilMs) > 0) {
    udpHoldoffUntilMs = until;
  }
  if (!wasActive) udpHoldoffEvents = udpHoldoffEvents + 1;
}

bool ensureRpiHostIp() {
  if (rpiHostIpValid) return true;
  if (rpiHostIp.fromString(RPI_HOST)) {
    rpiHostIpValid = true;
    return true;
  }
  if (WiFi.hostByName(RPI_HOST, rpiHostIp) == 1) {
    rpiHostIpValid = true;
    return true;
  }
  return false;
}

bool isTransientSendErrno(int err) {
  return err == EAGAIN || err == EWOULDBLOCK || err == ENOMEM ||
         err == ENOBUFS;
}

bool udpErrorLooksLikeDeadSocket(int err) {
  return err == EBADF || err == ENOTSOCK || err == ENOTCONN || err == EPIPE;
}

void initializeBootId() {
  uint32_t words[4];
  for (uint8_t index = 0; index < 4; ++index) words[index] = esp_random();
  snprintf(bootId, sizeof(bootId), "%08lx%08lx%08lx%08lx",
           static_cast<unsigned long>(words[0]),
           static_cast<unsigned long>(words[1]),
           static_cast<unsigned long>(words[2]),
           static_cast<unsigned long>(words[3]));
}

// Blocking waits are used only during one-time hardware initialization. The
// runtime loop itself remains delay-free.
void setupWait(uint32_t milliseconds) {
  vTaskDelay(pdMS_TO_TICKS(milliseconds));
}

bool i2cPresent(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

bool thermalWriteRegister(uint8_t reg, uint8_t value) {
  if (thermalAddress == 0) return false;
  Wire.beginTransmission(thermalAddress);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool thermalReadRegister(uint8_t reg, uint8_t &value) {
  if (thermalAddress == 0) return false;
  Wire.beginTransmission(thermalAddress);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(thermalAddress, static_cast<uint8_t>(1)) != 1) {
    return false;
  }
  value = Wire.read();
  return true;
}

void initializeThermalCamera() {
  thermalStarted = false;
  thermalAddress = 0;

  // Reset first. A hung or just-powered MI48 often will not ACK 0x40 until
  // EN/RESET is pulsed; probing first then returning left Pi thermal STALE.
  digitalWrite(PIN_THERMAL_RESET, LOW);
  delayMicroseconds(100);
  digitalWrite(PIN_THERMAL_RESET, HIGH);
  setupWait(120);

  if (i2cPresent(THERMAL_ADDRESS_A)) {
    thermalAddress = THERMAL_ADDRESS_A;
  } else if (i2cPresent(THERMAL_ADDRESS_B)) {
    thermalAddress = THERMAL_ADDRESS_B;
  } else {
    static uint32_t lastMissLogMs = 0;
    const uint32_t now = millis();
    if (lastMissLogMs == 0 ||
        static_cast<uint32_t>(now - lastMissLogMs) >= THERMAL_I2C_MISS_LOG_MS) {
      lastMissLogMs = now;
      Serial.println("[thermal] ERROR: I2C address 0x40/0x41 not found");
    }
    return;
  }

  uint8_t evkTest = 0;
  if (!thermalReadRegister(REG_EVK_TEST, evkTest)) {
    Serial.println("[thermal] ERROR: register read failed");
    return;
  }

  // Non-bridge MI48 variants require the explicit sensor power-up command.
  if (evkTest != 0xFF) {
    if (!thermalWriteRegister(REG_SENSOR_POWERUP, 0x13)) {
      Serial.println("[thermal] ERROR: power-up command failed");
      return;
    }
    setupWait(100);
  }

  const uint32_t bootStarted = millis();
  uint8_t status = STATUS_BOOTING;
  while (static_cast<uint32_t>(millis() - bootStarted) < 3000) {
    if (!thermalReadRegister(REG_STATUS, status)) {
      Serial.println("[thermal] ERROR: status read failed");
      return;
    }
    if ((status & STATUS_BOOTING) == 0) break;
    setupWait(25);
  }
  if (status & STATUS_BOOTING) {
    Serial.println("[thermal] ERROR: boot timeout");
    return;
  }

  uint8_t fw1 = 0, fw2 = 0, sensorType = 0;
  thermalReadRegister(REG_FW_VERSION_1, fw1);
  thermalReadRegister(REG_FW_VERSION_2, fw2);
  thermalReadRegister(REG_SENSOR_TYPE, sensorType);

  thermalWriteRegister(REG_FRAME_MODE, 0x00);
  setupWait(30);
  if (!thermalWriteRegister(REG_FRAME_RATE, THERMAL_FRAME_RATE_DIVIDER) ||
      !thermalWriteRegister(REG_FRAME_MODE, MODE_CONTINUOUS)) {
    Serial.println("[thermal] ERROR: continuous stream start failed");
    return;
  }

  thermalStarted = true;
  lastThermalProgressMs = millis();
  lastRecoverCaptureAttempts = thermalCaptureAttempts;
  Serial.printf("[thermal] ready: addr=0x%02X type=%u fw=%u.%u.%u\n",
                static_cast<unsigned>(thermalAddress),
                static_cast<unsigned>(sensorType),
                static_cast<unsigned>((fw1 >> 4) & 0x0F),
                static_cast<unsigned>(fw1 & 0x0F),
                static_cast<unsigned>(fw2));
}

void maybeRecoverThermalCamera(uint32_t now) {
  if (!scheduleDue(now, lastThermalRecoverMs, THERMAL_RECOVER_PERIOD_MS)) {
    return;
  }
  // 'c'/'u' off also freezes capture progress. Do not GPIO-reset over that.
  if (!thermalCaptureEnabled || !thermalUdpEnabled) {
    return;
  }
  if (thermalStarted) {
    if (thermalCaptureAttempts != lastRecoverCaptureAttempts) {
      lastRecoverCaptureAttempts = thermalCaptureAttempts;
      lastThermalProgressMs = now;
      return;
    }
    if (static_cast<uint32_t>(now - lastThermalProgressMs) <
        THERMAL_STALE_REINIT_MS) {
      return;
    }
  }

  Serial.printf(
      "[thermal] auto-reinit started=%d cap_attempts=%lu (GPIO25 reset then I2C)\n",
      thermalStarted ? 1 : 0,
      static_cast<unsigned long>(thermalCaptureAttempts));
  initializeThermalCamera();
}

// MI48 frame CRC: CRC-16/CCITT-FALSE, polynomial 0x1021, initial 0xFFFF.
// The vendor driver calculates it over the native uint16 pixel buffer, so
// each received word is fed low byte first and then high byte.
uint16_t thermalFrameCrc(const uint16_t *pixels, size_t count) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < count; ++i) {
    const uint8_t bytes[2] = {
        static_cast<uint8_t>(pixels[i] & 0xFF),
        static_cast<uint8_t>(pixels[i] >> 8)};
    for (uint8_t value : bytes) {
      crc ^= static_cast<uint16_t>(value) << 8;
      for (uint8_t bit = 0; bit < 8; ++bit) {
        crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                             : static_cast<uint16_t>(crc << 1);
      }
    }
  }
  return crc;
}

bool thermalDataReady(uint32_t now) {
  // Counted per source: a READY line left floating, or driven permanently
  // high, reports ready on every one of the ~40k loop iterations per second
  // and would flood the UDP path with garbage frames. Comparing ready_pin
  // against the ~6 accepted frames per second is what makes that visible.
  if (digitalRead(PIN_THERMAL_READY) == HIGH) {
    ++thermalReadyByPin;
    return true;
  }

  // I2C polling is a fallback and also makes a disconnected READY wire visible.
  if (static_cast<uint32_t>(now - lastThermalStatusPollMs) < 25) return false;
  lastThermalStatusPollMs = now;
  uint8_t status = 0;
  if (!thermalReadRegister(REG_STATUS, status)) {
    ++thermalStatusQueryFailures;
    return false;
  }
  if ((status & STATUS_DATA_READY) == 0) return false;
  ++thermalReadyByI2c;
  return true;
}

void captureThermalIfReady(uint32_t now) {
  if (!thermalStarted || !thermalCaptureEnabled) return;
  // 'u' still gates SPI so turning UDP off also stops the ~20 ms/frame
  // capture that was starving MR60. TCP unhealthy used to stand down
  // capture too; that left Pi thermal STALE while reconnecting even though
  // UDP send already yields on TCP_CRITICAL_BIT. Keep capturing; the send
  // path still defers while the session is down.
  if (!thermalUdpEnabled) return;
  if (!thermalDataReady(now)) return;

  ++thermalCaptureAttempts;
  const uint32_t captureStartedMs = millis();

  thermalSpi.beginTransaction(
      SPISettings(THERMAL_SPI_HZ, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_THERMAL_CS, LOW);
  delayMicroseconds(100);

  // MI48xx returns every word MSB first. Reading bytes explicitly avoids host
  // endianness ambiguity in SPI.transfer16().
  for (size_t i = 0; i < THERMAL_CAPTURE_WORDS; ++i) {
    const uint8_t highByte = thermalSpi.transfer(0x00);
    const uint8_t lowByte = thermalSpi.transfer(0x00);
    thermalCapture[i] = (static_cast<uint16_t>(highByte) << 8) | lowByte;
  }

  delayMicroseconds(100);
  digitalWrite(PIN_THERMAL_CS, HIGH);
  thermalSpi.endTransaction();

  // 10080 single-byte SPI transfers block loop() outright. If this grows past
  // the telemetry period it starves publishTelemetrySnapshot() on core 1, which
  // the Pi sees as a late packet just as surely as a stalled socket write.
  // At 1 fps this burst happens once per second instead of twice.
  const uint32_t captureMs =
      static_cast<uint32_t>(millis() - captureStartedMs);
  if (captureMs > thermalCaptureMaxMs) thermalCaptureMaxMs = captureMs;

  ThermalTxFrame &frame = thermalProducerFrame;
  frame.uptimeMs = millis();
  frame.minimumRaw = UINT16_MAX;
  frame.maximumRaw = 0;

  for (size_t i = 0; i < THERMAL_PIXEL_COUNT; ++i) {
    const uint16_t raw = thermalCapture[THERMAL_HEADER_WORDS + i];
    frame.pixels[i] = raw;
    if (raw < frame.minimumRaw) frame.minimumRaw = raw;
    if (raw > frame.maximumRaw) frame.maximumRaw = raw;
  }

  const uint16_t expectedCrc = thermalCapture[7];
  const uint16_t actualCrc = thermalFrameCrc(
      thermalCapture + THERMAL_HEADER_WORDS, THERMAL_PIXEL_COUNT);
  if (actualCrc != expectedCrc) {
    ++thermalCrcErrors;
    if (thermalCrcErrors <= 3 || thermalCrcErrors % 25 == 0) {
      Serial.printf(
          "[thermal] dropped CRC frame: header=0x%04X calc=0x%04X "
          "errors=%lu\n",
          static_cast<unsigned>(expectedCrc),
          static_cast<unsigned>(actualCrc),
          static_cast<unsigned long>(thermalCrcErrors));
    }
    return;
  }

  if (thermalCapture[5] != frame.maximumRaw ||
      thermalCapture[6] != frame.minimumRaw) {
    ++thermalRangeErrors;
    if (thermalRangeErrors <= 3 || thermalRangeErrors % 25 == 0) {
      Serial.printf(
          "[thermal] dropped header-range frame: header=%u..%u "
          "calc=%u..%u errors=%lu\n",
          static_cast<unsigned>(thermalCapture[6]),
          static_cast<unsigned>(thermalCapture[5]),
          static_cast<unsigned>(frame.minimumRaw),
          static_cast<unsigned>(frame.maximumRaw),
          static_cast<unsigned long>(thermalRangeErrors));
    }
    return;
  }

  frame.frameSequence = ++thermalSequence;
  // Queue length is one, so this replaces an unsent old frame under congestion.
  xQueueOverwrite(thermalQueue, &frame);
}

// Winsen checksum over bytes 1..7 of a 9-byte UART frame.
uint8_t mhz19Checksum(const uint8_t *frame) {
  uint8_t sum = 0;
  for (uint8_t index = 1; index <= 7; ++index) {
    sum = static_cast<uint8_t>(sum + frame[index]);
  }
  return static_cast<uint8_t>(~sum + 1);
}

void drainMhz19Rx() {
  while (mhz19Serial.available() > 0) {
    (void)mhz19Serial.read();
  }
}

void sendMhz19ReadCommand(uint32_t now) {
  drainMhz19Rx();
  mhz19Serial.write(MHZ19_READ_CMD, sizeof(MHZ19_READ_CMD));
  mhz19Txn = Mhz19Txn::AWAIT_RESPONSE;
  mhz19RequestSentMs = now;
}

bool readMhz19Frame(uint8_t *frame) {
  size_t got = 0;
  while (got < 9 && mhz19Serial.available() > 0) {
    frame[got++] = static_cast<uint8_t>(mhz19Serial.read());
  }
  return got == 9;
}

void acceptMhz19Sample(uint32_t now, uint16_t ppm) {
  co2LastUartPpm = ppm;
  ++co2AcceptedSamples;

  // Preheat packets still go out on TCP, but they are not model measurements.
  if (co2InPreheat(now)) return;
  // 0 ppm is fail-closed, same as the SCD4x v2 path. Values above the
  // documented optional 10000 ppm class are treated as protocol garbage.
  if (ppm == 0 || ppm > CO2_PPM_MAX_ACCEPTED) return;

  co2Ppm = ppm;
  lastCo2Ms = now;
  ++co2MeasurementEventId;
  if (co2MeasurementEventId == 0) ++co2MeasurementEventId;
  co2MeasurementMonotonicMs = now;
  co2MeasurementEventValid = true;
}

void handleMhz19Frame(uint32_t now, const uint8_t *frame) {
  if (frame[0] != 0xFF || frame[1] != 0x86 ||
      mhz19Checksum(frame) != frame[8]) {
    ++co2ChecksumFailures;
    return;
  }
  const uint16_t ppm =
      (static_cast<uint16_t>(frame[2]) << 8) | frame[3];
  acceptMhz19Sample(now, ppm);
}

void pollCo2(uint32_t now) {
  if (mhz19Txn == Mhz19Txn::IDLE) {
    if (!scheduleDue(now, lastCo2PollMs, CO2_UART_SAMPLE_PERIOD_MS)) return;
    sendMhz19ReadCommand(now);
    return;
  }

  if (mhz19Serial.available() >= 9) {
    uint8_t frame[9];
    if (!readMhz19Frame(frame)) {
      ++co2ShortFrameFailures;
      drainMhz19Rx();
      mhz19Txn = Mhz19Txn::IDLE;
      return;
    }
    handleMhz19Frame(now, frame);
    drainMhz19Rx();
    mhz19Txn = Mhz19Txn::IDLE;
    return;
  }

  if (deadlineReached(now, mhz19RequestSentMs + CO2_UART_RESPONSE_TIMEOUT_MS)) {
    ++co2TimeoutFailures;
    if (mhz19Serial.available() > 0) ++co2ShortFrameFailures;
    drainMhz19Rx();
    mhz19Txn = Mhz19Txn::IDLE;
  }
}

void applyMmWaveReports(uint32_t now, bool &gotBreath, bool &gotHeart) {
  float nextTotalPhase = NAN;
  float nextBreathPhase = NAN;
  float nextHeartPhase = NAN;
  if (mmWave.getHeartBreathPhases(nextTotalPhase, nextBreathPhase,
                                  nextHeartPhase) &&
      isfinite(nextTotalPhase) && isfinite(nextBreathPhase) &&
      isfinite(nextHeartPhase)) {
    totalPhase = nextTotalPhase;
    breathPhase = nextBreathPhase;
    heartPhase = nextHeartPhase;
    lastPhaseMs = millis();
    phaseSamplePresent = true;
    ++phaseSequence;
  }

  float value = 0.0f;
  if (mmWave.getBreathRate(value) && isfinite(value)) {
    respirationRate = value;
    lastRespirationMs = now;
    gotBreath = true;
  }
  if (mmWave.getHeartRate(value) && isfinite(value)) {
    heartRate = value;
    lastHeartMs = now;
    gotHeart = true;
  }

  bool presenceValue = false;
  if (mmWave.takePresence(presenceValue)) {
    humanDetectedRaw = presenceValue;
    lastPresenceMs = now;
  }
}

void pollMmWave(uint32_t now) {
  // Do not call mmWave.update(). Seeed fetch() uses a static startFrame that
  // never times out, so one false 0x01 locks the parser for the rest of the
  // boot (UART bytes present, mmw_ok=0, phase=nan). Assemble locally.
  bool parsed = false;
  bool gotBreath = false;
  bool gotHeart = false;

  if (mmWaveInFrame &&
      static_cast<uint32_t>(now - mmWaveFrameStartedMs) >
          MMWAVE_FRAME_STALE_MS) {
    mmWaveInFrame = false;
    mmWaveFrameLen = 0;
  }

  uint8_t hexDump[MMWAVE_HEXDUMP_BYTES];
  uint8_t hexDumpLen = 0;
  const bool dumpThisBoot =
      !mmWaveHexDumped && now >= 5000 && mmWaveUpdateSuccesses == 0;

  uint16_t drained = 0;
  while (mmWaveSerial.available() > 0 && drained < 512) {
    const int raw = mmWaveSerial.read();
    if (raw < 0) break;
    const uint8_t byte = static_cast<uint8_t>(raw);
    ++drained;
    if (dumpThisBoot && hexDumpLen < MMWAVE_HEXDUMP_BYTES) {
      hexDump[hexDumpLen++] = byte;
    }

    if (!mmWaveInFrame) {
      if (byte != MMWAVE_SOF_BYTE) continue;
      mmWaveInFrame = true;
      mmWaveFrameLen = 0;
      mmWaveFrameBuf[mmWaveFrameLen++] = byte;
      mmWaveFrameStartedMs = now;
      continue;
    }

    if (mmWaveFrameLen >= sizeof(mmWaveFrameBuf)) {
      mmWaveInFrame = false;
      mmWaveFrameLen = 0;
      if (byte == MMWAVE_SOF_BYTE) {
        mmWaveInFrame = true;
        mmWaveFrameBuf[mmWaveFrameLen++] = byte;
        mmWaveFrameStartedMs = now;
      }
      continue;
    }

    mmWaveFrameBuf[mmWaveFrameLen++] = byte;
    if (mmWaveFrameLen < MMWAVE_FRAME_HEADER_SIZE) continue;

    const uint16_t dataLen =
        (static_cast<uint16_t>(mmWaveFrameBuf[3]) << 8) | mmWaveFrameBuf[4];
    if (dataLen > MMWAVE_MAX_DATA_SIZE) {
      mmWaveInFrame = false;
      mmWaveFrameLen = 0;
      if (byte == MMWAVE_SOF_BYTE) {
        mmWaveInFrame = true;
        mmWaveFrameBuf[mmWaveFrameLen++] = byte;
        mmWaveFrameStartedMs = now;
      }
      continue;
    }

    const size_t need = MMWAVE_FRAME_HEADER_SIZE + dataLen + 1;
    if (mmWaveFrameLen < need) continue;
    if (mmWave.ingestFrame(mmWaveFrameBuf, mmWaveFrameLen)) {
      parsed = true;
      ++mmWaveUpdateSuccesses;
      lastMmWaveUpdateMs = now;
      applyMmWaveReports(now, gotBreath, gotHeart);
    }
    mmWaveInFrame = false;
    mmWaveFrameLen = 0;
  }

  if (dumpThisBoot && hexDumpLen > 0) {
    mmWaveHexDumped = true;
    Serial.printf("[mmw-rx] n=%u", static_cast<unsigned>(hexDumpLen));
    for (uint8_t i = 0; i < hexDumpLen; ++i) {
      Serial.printf(" %02X", hexDump[i]);
    }
    Serial.printf(" (sof should be %02X)\n", MMWAVE_SOF_BYTE);
  } else if (!mmWaveHexDumped && now >= 8000 && mmWaveUpdateSuccesses == 0) {
    mmWaveHexDumped = true;
    Serial.printf(
        "[mmw-rx] n=0 after 8s -- UART2 silent RX=%d TX=%d baud=%lu\n",
        PIN_MMWAVE_RX, PIN_MMWAVE_TX, static_cast<unsigned long>(MMWAVE_BAUD));
  }

  if (!parsed && mmWaveSerial.available() > 0) {
    ++mmWaveUpdateMisses;
  }
  if (gotBreath || gotHeart) {
    static uint32_t lastMmwSampleLogMs = 0;
    if (lastMmwSampleLogMs == 0 ||
        static_cast<uint32_t>(now - lastMmwSampleLogMs) >= 2000) {
      lastMmwSampleLogMs = now;
      Serial.printf(
          "[mmw] breath=%.2f heart=%.2f presence=%s phase=%.4f "
          "phase_seq=%lu uart=%u\n",
          respirationRate, heartRate,
          lastPresenceMs == 0 ? "unknown"
                              : (humanDetectedRaw ? "true" : "false"),
          breathPhase, static_cast<unsigned long>(phaseSequence),
          static_cast<unsigned>(mmWaveSerial.available()));
    }
  }
}

void publishTelemetrySnapshot(uint32_t now) {
  if (!scheduleDue(now, lastTelemetryMs, TELEMETRY_PERIOD_MS)) return;

  TelemetrySnapshot snapshot{};
  snapshot.sequence = ++telemetrySequence;
  snapshot.uptimeMs = now;
  snapshot.respirationRate = respirationRate;
  snapshot.heartRate = heartRate;
  snapshot.co2Ppm = co2Ppm;
  snapshot.pirMotion = pirMotion;
  snapshot.respirationValid = isFresh(lastRespirationMs, now, MMWAVE_STALE_MS);
  snapshot.heartValid = isFresh(lastHeartMs, now, MMWAVE_STALE_MS);
  snapshot.co2Preheat = co2InPreheat(now);
  // valid.co2 is last-success-within-stale-limit, not a new-event claim.
  snapshot.co2Valid =
      !snapshot.co2Preheat && isFresh(lastCo2Ms, now, CO2_STALE_MS);
  // The event identity is valid only while the corresponding inferred UART
  // sample is fresh. Once stale or in preheat, fail closed with the
  // protocol-required zero tuple instead of republishing an old sample as
  // current. Same non-zero event_id on later 1 Hz snapshots is a cached
  // retransmission -- Pi slope keys on event_id, not packet seq.
  const bool co2EventFresh = snapshot.co2Valid && co2MeasurementEventValid;
  snapshot.co2MeasurementEventId = co2EventFresh ? co2MeasurementEventId : 0;
  snapshot.co2MeasurementMonotonicMs =
      co2EventFresh ? co2MeasurementMonotonicMs : 0;
  snapshot.co2MeasurementEventValid = co2EventFresh;
  // isFresh() also rejects the never-observed case (lastPresenceMs == 0), so a
  // node whose radar never reported occupancy publishes null rather than false.
  snapshot.humanDetectedRaw = humanDetectedRaw;
  snapshot.humanDetectedKnown = isFresh(lastPresenceMs, now,
                                        PRESENCE_MAX_AGE_MS);
  snapshot.phaseSamplePresent = phaseSamplePresent;
  snapshot.totalPhase = totalPhase;
  snapshot.breathPhase = breathPhase;
  snapshot.heartPhase = heartPhase;
  snapshot.phaseTimestampMs = phaseSamplePresent ? lastPhaseMs : 0;
  snapshot.phaseSequence = phaseSequence;
  xQueueOverwrite(telemetryQueue, &snapshot);
}

// Big-endian integer encoders keep the protocol independent of CPU endianness.
void putU16(uint8_t *destination, uint16_t value) {
  destination[0] = static_cast<uint8_t>(value >> 8);
  destination[1] = static_cast<uint8_t>(value);
}

void putU32(uint8_t *destination, uint32_t value) {
  destination[0] = static_cast<uint8_t>(value >> 24);
  destination[1] = static_cast<uint8_t>(value >> 16);
  destination[2] = static_cast<uint8_t>(value >> 8);
  destination[3] = static_cast<uint8_t>(value);
}

void makePacketHeader(uint8_t *header, uint8_t type, uint32_t sequence,
                      uint32_t payloadLength) {
  memcpy(header, "SNST", 4);
  header[4] = PROTOCOL_VERSION;
  header[5] = type;
  putU16(header + 6, 0);  // flags: reserved for protocol v1
  putU32(header + 8, sequence);
  putU32(header + 12, payloadLength);
}

// Runs only in the network task, so a slow peer can never stall sensor capture.
// Does not call WiFiClient.write(): Arduino-ESP32 3.x can block that for 10 s.
bool writeAll(WiFiClient &client, const uint8_t *data, size_t length,
              TcpWriteReport &report) {
  report = TcpWriteReport{};
  const int sock = client.fd();
  if (sock < 0) {
    report.connectedAtEnd = false;
    report.lastErrno = EBADF;
    return false;
  }

  size_t sent = 0;
  const uint32_t startedMs = millis();
  uint32_t lastProgress = startedMs;

  // One DONTWAIT send from zero. Retrying EAGAIN for 40-200 ms is what
  // produced wrote=186/794: a sliver of TX room appears and a SNST prefix
  // goes on the wire, then this node has to close. Skip instead.
  errno = 0;
  const int first = lwip_send(sock, data, length, MSG_DONTWAIT);
  if (first <= 0) {
    const int err = first < 0 ? errno : ENOTCONN;
    report.zeroWrites = 1;
    report.lastErrno = err;
    report.elapsedMs = static_cast<uint32_t>(millis() - startedMs);
    report.connectedAtEnd = client.connected();
    return false;
  }
  if (static_cast<size_t>(first) < length) ++report.partialWrites;
  sent = static_cast<size_t>(first);
  lastProgress = millis();

  while (sent < length) {
    const uint32_t elapsedMs =
        static_cast<uint32_t>(millis() - startedMs);
    if (elapsedMs > TCP_WRITE_DEADLINE_MS) {
      report.bytesWritten = sent;
      report.elapsedMs = elapsedMs;
      if (report.lastErrno == 0) report.lastErrno = ETIMEDOUT;
      report.connectedAtEnd = client.connected();
      return false;
    }
    if (!client.connected()) {
      report.bytesWritten = sent;
      report.elapsedMs = elapsedMs;
      report.connectedAtEnd = false;
      if (report.lastErrno == 0) report.lastErrno = ENOTCONN;
      return false;
    }

    const size_t remaining = length - sent;
    errno = 0;
    const int written = lwip_send(sock, data + sent, remaining, MSG_DONTWAIT);
    if (written > 0) {
      if (static_cast<size_t>(written) < remaining) ++report.partialWrites;
      sent += static_cast<size_t>(written);
      lastProgress = millis();
    } else {
      ++report.zeroWrites;
      const int err = written < 0 ? errno : ENOTCONN;
      report.lastErrno = err;
      const uint32_t stallMs =
          static_cast<uint32_t>(millis() - lastProgress);
      if (stallMs > report.longestStallMs) report.longestStallMs = stallMs;
      if (written == 0 || !isTransientSendErrno(err)) {
        report.bytesWritten = sent;
        report.elapsedMs = static_cast<uint32_t>(millis() - startedMs);
        report.connectedAtEnd = client.connected();
        return false;
      }
      vTaskDelay(pdMS_TO_TICKS(2));
    }
  }

  report.bytesWritten = sent;
  report.elapsedMs = static_cast<uint32_t>(millis() - startedMs);
  report.connectedAtEnd = true;
  return true;
}

// The mutex wait is the ESP32-side price of the thermal stream: precisely the
// time this task spends unable to touch the socket because a UDP datagram is
// in flight. Reporting it turns "the connection dropped" into a number.
bool beginTcpCritical(uint32_t &waitMs) {
  const uint32_t startedMs = millis();
  xEventGroupSetBits(networkEvents, TCP_CRITICAL_BIT);
  const bool acquired =
      xSemaphoreTake(networkTxMutex, pdMS_TO_TICKS(TCP_MUTEX_MAX_WAIT_MS)) ==
      pdTRUE;
  waitMs = static_cast<uint32_t>(millis() - startedMs);
  if (waitMs > tcpMutexWaitMaxMs) tcpMutexWaitMaxMs = waitMs;
  if (waitMs >= TCP_MUTEX_WARN_MS) tcpMutexWaitSlow = tcpMutexWaitSlow + 1;
  if (acquired) return true;
  xEventGroupClearBits(networkEvents, TCP_CRITICAL_BIT);
  return false;
}

void endTcpCritical() {
  xSemaphoreGive(networkTxMutex);
  xEventGroupClearBits(networkEvents, TCP_CRITICAL_BIT);
}

void formatNullableFloat(char *output, size_t outputSize, bool valid,
                         float value) {
  if (valid && isfinite(value)) {
    const int written = snprintf(output, outputSize, "%.2f", value);
    // A truncated number stays syntactically valid JSON but carries the wrong
    // magnitude, so fail closed to null rather than publish it.
    if (written > 0 && static_cast<size_t>(written) < outputSize) return;
  }
  strlcpy(output, "null", outputSize);
}

void formatNullablePhase(char *output, size_t outputSize, bool valid,
                         float value) {
  if (valid && isfinite(value)) {
    const int written = snprintf(output, outputSize, "%.6f", value);
    if (written > 0 && static_cast<size_t>(written) < outputSize) return;
  }
  strlcpy(output, "null", outputSize);
}

void formatNullableU32(char *output, size_t outputSize, bool valid,
                       uint32_t value) {
  if (valid) {
    const int written = snprintf(output, outputSize, "%lu",
                                 static_cast<unsigned long>(value));
    if (written > 0 && static_cast<size_t>(written) < outputSize) return;
  }
  strlcpy(output, "null", outputSize);
}

bool sendTelemetry(WiFiClient &client, const TelemetrySnapshot &snapshot,
                   size_t &payloadLength, TcpWriteReport &report) {
  payloadLength = 0;
  report = TcpWriteReport{};
  char respiration[20], heart[20], co2[8];
  formatNullableFloat(respiration, sizeof(respiration),
                      snapshot.respirationValid, snapshot.respirationRate);
  formatNullableFloat(heart, sizeof(heart), snapshot.heartValid,
                      snapshot.heartRate);
  if (snapshot.co2Valid) {
    const int written = snprintf(co2, sizeof(co2), "%u",
                                 static_cast<unsigned>(snapshot.co2Ppm));
    if (written <= 0 || static_cast<size_t>(written) >= sizeof(co2)) {
      strlcpy(co2, "null", sizeof(co2));
    }
  } else {
    strlcpy(co2, "null", sizeof(co2));
  }

  const char *humanDetectedText = snapshot.humanDetectedKnown
                                      ? (snapshot.humanDetectedRaw ? "true"
                                                                   : "false")
                                      : "null";

  char totalPhaseText[32], breathPhaseText[32], heartPhaseText[32];
  char breathRateRawText[20], phaseAgeText[20], phaseTimestampText[20];
  char phaseSequenceText[20];
  const uint32_t sendNow = millis();
  const uint32_t phaseAgeMs = snapshot.phaseSamplePresent
                                  ? static_cast<uint32_t>(
                                        sendNow - snapshot.phaseTimestampMs)
                                  : 0;
  const bool phaseFresh = snapshot.phaseSamplePresent &&
                          phaseAgeMs < PHASE_MAX_AGE_MS;
  formatNullablePhase(totalPhaseText, sizeof(totalPhaseText), phaseFresh,
                      snapshot.totalPhase);
  formatNullablePhase(breathPhaseText, sizeof(breathPhaseText), phaseFresh,
                      snapshot.breathPhase);
  formatNullablePhase(heartPhaseText, sizeof(heartPhaseText), phaseFresh,
                      snapshot.heartPhase);
  formatNullableFloat(breathRateRawText, sizeof(breathRateRawText),
                      snapshot.respirationValid, snapshot.respirationRate);
  formatNullableU32(phaseAgeText, sizeof(phaseAgeText),
                    snapshot.phaseSamplePresent, phaseAgeMs);
  formatNullableU32(phaseTimestampText, sizeof(phaseTimestampText),
                    snapshot.phaseSamplePresent, snapshot.phaseTimestampMs);
  formatNullableU32(phaseSequenceText, sizeof(phaseSequenceText),
                    snapshot.phaseSamplePresent, snapshot.phaseSequence);

  // 1.3.0 mmwave phase object plus MH-Z19B event-identity fields.
  // Truncation is fail-closed.
  char json[1536];
  const int length = snprintf(
      json, sizeof(json),
      "{\"schema\":\"safenest.telemetry.v1\",\"device_id\":\"%s\","
      "\"boot_id\":\"%s\",\"seq\":%lu,\"uptime_ms\":%lu,"
      "\"firmware_version\":\"%s\","
      "\"resp_rate_bpm\":%s,\"heart_rate_bpm\":%s,\"co2_ppm\":%s,"
      "\"co2_sensor_model\":\"%s\","
      "\"co2_event_identity_class\":\"%s\","
      "\"co2_measurement_event_id\":%lu,"
      "\"co2_measurement_monotonic_ms\":%lu,"
      "\"co2_measurement_event_valid\":%s,\"co2_preheat\":%s,"
      "\"co2_preheat_complete\":%s,"
      "\"pir_motion\":%s,"
      "\"valid\":{\"respiration\":%s,\"heart\":%s,\"co2\":%s},"
      "\"mmwave\":{\"breath_phase\":%s,\"total_phase\":%s,"
      "\"heart_phase\":%s,\"breath_rate_raw\":%s,"
      "\"human_detected_raw\":%s,"
      "\"phase_age_ms\":%s,\"ts_monotonic_ms\":%s,\"seq\":%s,"
      "\"firmware_version\":\"%s\",\"schema_version\":\"%s\"}}",
      DEVICE_ID, bootId, static_cast<unsigned long>(snapshot.sequence),
      static_cast<unsigned long>(snapshot.uptimeMs), NODE_FIRMWARE_VERSION,
      respiration, heart, co2, CO2_SENSOR_MODEL, CO2_EVENT_IDENTITY_CLASS,
      static_cast<unsigned long>(snapshot.co2MeasurementEventId),
      static_cast<unsigned long>(snapshot.co2MeasurementMonotonicMs),
      snapshot.co2MeasurementEventValid ? "true" : "false",
      snapshot.co2Preheat ? "true" : "false",
      // Pi maps firmware `co2_preheat` onto `co2_preheat_complete` when the
      // canonical field is missing, without inverting it. co2_preheat means
      // "still warming" (true for 180 s). C-B6 only ingests when
      // preheat_complete is true, so after warmup the alias permanently
      // blocks the model. Send the canonical bit ourselves: complete = !preheat.
      snapshot.co2Preheat ? "false" : "true",
      snapshot.pirMotion ? "true" : "false",
      snapshot.respirationValid ? "true" : "false",
      snapshot.heartValid ? "true" : "false",
      snapshot.co2Valid ? "true" : "false", breathPhaseText, totalPhaseText,
      heartPhaseText, breathRateRawText, humanDetectedText, phaseAgeText,
      phaseTimestampText, phaseSequenceText, NODE_FIRMWARE_VERSION,
      MMWAVE_SCHEMA_VERSION);
  if (length <= 0 || static_cast<size_t>(length) >= sizeof(json)) return false;

  payloadLength = static_cast<size_t>(length);
  uint8_t packet[PACKET_HEADER_SIZE + sizeof(json)];
  makePacketHeader(packet, PACKET_TELEMETRY_JSON, snapshot.sequence,
                   static_cast<uint32_t>(length));
  memcpy(packet + PACKET_HEADER_SIZE, json, payloadLength);

  return writeAll(client, packet, PACKET_HEADER_SIZE + payloadLength, report);
}

uint8_t thermalPayloadByte(const ThermalTxFrame &frame, const uint8_t *meta,
                           size_t offset) {
  if (offset < THERMAL_META_SIZE) return meta[offset];
  const size_t pixelByte = offset - THERMAL_META_SIZE;
  const uint16_t pixel = frame.pixels[pixelByte / 2];
  return (pixelByte & 1) ? static_cast<uint8_t>(pixel)
                         : static_cast<uint8_t>(pixel >> 8);
}

uint32_t thermalFrameCrc32(const ThermalTxFrame &frame, const uint8_t *meta) {
  uint32_t crc = 0xFFFFFFFF;
  for (size_t offset = 0; offset < THERMAL_PAYLOAD_SIZE; ++offset) {
    crc ^= thermalPayloadByte(frame, meta, offset);
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320U & (0U - (crc & 1U)));
    }
  }
  return ~crc;
}

// Rate-limited: Serial.printf() blocks for roughly 90 us per character at
// 115200 baud, so an unthrottled warning would itself become the stall it is
// trying to report.
void logSlowDatagram(uint32_t frameSequence, uint16_t chunkIndex,
                     uint32_t durationMs) {
  static uint32_t lastLogMs = 0;
  const uint32_t now = millis();
  if (lastLogMs != 0 && static_cast<uint32_t>(now - lastLogMs) < 1000) return;
  lastLogMs = now;
  Serial.printf(
      "[udp-slow] frame=%lu chunk=%u sendto_ms=%lu errno=%ld -- the TCP task "
      "cannot send for this long\n",
      static_cast<unsigned long>(frameSequence),
      static_cast<unsigned>(chunkIndex),
      static_cast<unsigned long>(durationMs),
      static_cast<long>(udpLastErrno));
}

// Park this task until TCP is not in a write/connect, without holding the TX
// mutex. The higher-priority TCP task can then take the mutex and send. false
// means the remaining chunks of this frame should be abandoned.
bool yieldRadioToTcp(uint32_t maxWaitMs) {
  const uint32_t startedMs = millis();
  bool waited = false;
  for (;;) {
    if (!tcpLinkHealthy) return false;
    if ((xEventGroupGetBits(networkEvents) & TCP_CRITICAL_BIT) == 0) {
      if (waited) thermalTcpYields = thermalTcpYields + 1;
      return true;
    }
    if (static_cast<uint32_t>(millis() - startedMs) >= maxWaitMs) {
      return false;
    }
    waited = true;
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// Rebuild the thermal UDP socket only when the fd itself looks dead. Periodic
// stop()/begin() was heap churn, not pbuf recovery: the 1460-byte TX buffer
// is malloc'd on begin() and freed on stop().
bool reclaimUdpHeap(WiFiUDP &udp, bool &udpStarted) {
  if ((xEventGroupGetBits(networkEvents) & TCP_CRITICAL_BIT) != 0) {
    heapReclaimSkippedTcp = heapReclaimSkippedTcp + 1;
    return false;
  }
  const uint32_t heapBefore = ESP.getFreeHeap();
  if (udpStarted) {
    udp.stop();
    udpStarted = false;
  }
  udpStarted = udp.begin(0);
  heapReclaims = heapReclaims + 1;
  static uint32_t lastLogMs = 0;
  const uint32_t now = millis();
  if (lastLogMs == 0 || static_cast<uint32_t>(now - lastLogMs) >= 10000) {
    lastLogMs = now;
    Serial.printf(
        "[heap] reclaim=%lu skipped_tcp=%lu heap_before=%lu heap_after=%lu "
        "max_alloc=%lu udp_up=%d\n",
        static_cast<unsigned long>(heapReclaims),
        static_cast<unsigned long>(heapReclaimSkippedTcp),
        static_cast<unsigned long>(heapBefore),
        static_cast<unsigned long>(ESP.getFreeHeap()),
        static_cast<unsigned long>(ESP.getMaxAllocHeap()),
        udpStarted ? 1 : 0);
  }
  return udpStarted;
}

ThermalSendResult sendThermalUdp(WiFiUDP &udp,
                                 const ThermalTxFrame &frame) {
  constexpr TickType_t MUTEX_TIMEOUT = pdMS_TO_TICKS(1000);
  // Runtime kill switch. Dropping the frame here rather than upstream leaves
  // capture, CRC checking and queueing byte-for-byte identical, so toggling it
  // isolates the network transmit path and nothing else.
  if (!thermalUdpEnabled) return ThermalSendResult::Suppressed;
  // Back-pressure, checked before the CRC32 pass so a deferred frame costs
  // nothing: 9936 bytes of table-free CRC is not free at 2 fps, and there is no
  // point spending it on a frame that will not be sent.
  if (!tcpLinkHealthy) return ThermalSendResult::Deferred;
  if (udpHoldoffActive(millis())) return ThermalSendResult::Deferred;
  uint8_t meta[THERMAL_META_SIZE];
  putU16(meta + 0, static_cast<uint16_t>(THERMAL_WIDTH));
  putU16(meta + 2, static_cast<uint16_t>(THERMAL_HEIGHT));
  putU32(meta + 4, frame.frameSequence);
  putU32(meta + 8, frame.uptimeMs);
  putU16(meta + 12, frame.minimumRaw);
  putU16(meta + 14, frame.maximumRaw);
  const uint32_t crc32 = thermalFrameCrc32(frame, meta);

  for (uint16_t chunkIndex = 0; chunkIndex < THERMAL_UDP_CHUNK_COUNT;) {
    if (!yieldRadioToTcp(UDP_YIELD_TO_TCP_MAX_MS)) {
      return tcpLinkHealthy ? ThermalSendResult::Preempted
                            : ThermalSendResult::Deferred;
    }
    if (udpHoldoffActive(millis())) {
      return chunkIndex == 0 ? ThermalSendResult::Deferred
                             : ThermalSendResult::Preempted;
    }

    const size_t offset = chunkIndex * THERMAL_UDP_CHUNK_SIZE;
    const uint16_t length = static_cast<uint16_t>(
        min(THERMAL_UDP_CHUNK_SIZE, THERMAL_PAYLOAD_SIZE - offset));
    uint8_t *header = thermalUdpDatagram;
    memcpy(header, THERMAL_UDP_MAGIC, 4);
    header[4] = THERMAL_UDP_VERSION;
    header[5] = PACKET_THERMAL_U16_BE;
    putU16(header + 6, static_cast<uint16_t>(THERMAL_UDP_HEADER_SIZE));
    putU32(header + 8, frame.frameSequence);
    putU16(header + 12, chunkIndex);
    putU16(header + 14, THERMAL_UDP_CHUNK_COUNT);
    putU32(header + 16, static_cast<uint32_t>(THERMAL_PAYLOAD_SIZE));
    putU32(header + 20, static_cast<uint32_t>(offset));
    putU16(header + 24, length);
    putU16(header + 26, 0);
    putU32(header + 28, crc32);
    for (uint16_t index = 0; index < length; ++index) {
      thermalUdpDatagram[THERMAL_UDP_HEADER_SIZE + index] =
          thermalPayloadByte(frame, meta, offset + index);
    }

    if (xSemaphoreTake(networkTxMutex, MUTEX_TIMEOUT) != pdTRUE) {
      return ThermalSendResult::Preempted;
    }
    // TCP set the bit and is blocked on this mutex. Sending now would jump
    // the queue; give it back and retry the same chunk after the write.
    if ((xEventGroupGetBits(networkEvents) & TCP_CRITICAL_BIT) != 0) {
      xSemaphoreGive(networkTxMutex);
      continue;
    }

    // Timed inside the mutex, because this is exactly the window during which
    // the TCP task is locked out. Under Wi-Fi TX-buffer exhaustion sendto()
    // blocks rather than failing, so the duration matters more than the return
    // value: a datagram that takes 400 ms still reports sent=true while having
    // pushed the telemetry packet 400 ms closer to the Pi's deadline.
    errno = 0;
    const uint32_t datagramStartedMs = millis();
    const bool sent =
        udp.beginPacket(rpiHostIp, THERMAL_UDP_PORT) &&
        udp.write(thermalUdpDatagram, THERMAL_UDP_HEADER_SIZE + length) ==
            THERMAL_UDP_HEADER_SIZE + length &&
        udp.endPacket() == 1;
    const uint32_t datagramMs =
        static_cast<uint32_t>(millis() - datagramStartedMs);
    if (!sent) udpLastErrno = errno;
    if (datagramMs > udpDatagramMaxMs) udpDatagramMaxMs = datagramMs;
    if (sent) {
      udpDatagramsSent = udpDatagramsSent + 1;
      udpBytesSent = udpBytesSent + THERMAL_UDP_HEADER_SIZE + length;
    }
    if (datagramMs >= UDP_DATAGRAM_WARN_MS) {
      udpSlowDatagrams = udpSlowDatagrams + 1;
      logSlowDatagram(frame.frameSequence, chunkIndex, datagramMs);
    }
    xSemaphoreGive(networkTxMutex);
    if (!sent) return ThermalSendResult::Failed;

    chunkIndex = static_cast<uint16_t>(chunkIndex + 1);
    if (chunkIndex < THERMAL_UDP_CHUNK_COUNT) {
      vTaskDelay(pdMS_TO_TICKS(UDP_CHUNK_GAP_MS));
    }
  }
  return ThermalSendResult::Sent;
}

// Every socket teardown funnels through here, so a reconnect is never silent.
// The counters are sampled at the instant of the drop, which is what makes them
// attributable: a gap_max_ms at or above PI_PACKET_DEADLINE_MS means this node
// missed the Pi's receive deadline, and the Pi closed first.
void logTcpDrop(const char *reason, uint32_t sessionStartedMs,
                uint32_t sessionPackets, int pendingRxBytes) {
  const uint32_t now = millis();
  const uint32_t sessionMs =
      sessionStartedMs == 0 ? 0
                            : static_cast<uint32_t>(now - sessionStartedMs);
  tcpDrops = tcpDrops + 1;
  if (sessionMs < 10000) tcpShortSessions = tcpShortSessions + 1;
  if (!tcpErrorLogDue()) return;
  Serial.printf(
      "[tcp-drop] reason=%s session_ms=%lu session_packets=%lu "
      "since_last_send_ms=%ld gap_max_ms=%lu gap_late=%lu write_max_ms=%lu "
      "write_stalls=%lu partial_writes=%lu mutex_max_ms=%lu mutex_to=%lu "
      "tcp_errno=%ld rx_pending=%d udp_on=%d udp_dg=%lu udp_slow=%lu "
      "udp_dg_max_ms=%lu udp_frame_max_ms=%lu udp_errno=%ld rssi=%d "
      "heap=%lu min_heap=%lu max_alloc=%lu drops=%lu short_sessions=%lu "
      "write_to=%lu holdoff=%lu\n",
      reason,
      static_cast<unsigned long>(sessionMs),
      static_cast<unsigned long>(sessionPackets),
      lastTelemetrySentMs == 0
          ? -1L
          : static_cast<long>(now - lastTelemetrySentMs),
      static_cast<unsigned long>(telemetryGapMaxMs),
      static_cast<unsigned long>(telemetryGapOverDeadline),
      static_cast<unsigned long>(tcpWriteMaxMs),
      static_cast<unsigned long>(tcpWriteStalls),
      static_cast<unsigned long>(tcpPartialWrites),
      static_cast<unsigned long>(tcpMutexWaitMaxMs),
      static_cast<unsigned long>(tcpMutexTimeouts),
      static_cast<long>(tcpLastErrno),
      pendingRxBytes,
      thermalUdpEnabled ? 1 : 0,
      static_cast<unsigned long>(udpDatagramsSent),
      static_cast<unsigned long>(udpSlowDatagrams),
      static_cast<unsigned long>(udpDatagramMaxMs),
      static_cast<unsigned long>(udpFrameMaxMs),
      static_cast<long>(udpLastErrno),
      static_cast<int>(WiFi.RSSI()),
      static_cast<unsigned long>(ESP.getFreeHeap()),
      static_cast<unsigned long>(ESP.getMinFreeHeap()),
      static_cast<unsigned long>(ESP.getMaxAllocHeap()),
      static_cast<unsigned long>(tcpDrops),
      static_cast<unsigned long>(tcpShortSessions),
      static_cast<unsigned long>(tcpWriteTimeouts),
      static_cast<unsigned long>(udpHoldoffEvents));
}

void telemetryTcpTask(void *parameter) {
  (void)parameter;
  WiFiClient client;
  TelemetrySnapshot telemetry{};
  uint32_t lastDequeuedSequence = 0;
  uint32_t lastNetworkLogMs = 0;
  uint32_t lastConnectDurationMs = 0;
  uint32_t lastQueueWaitMs = 0;
  uint32_t lastMutexWaitMs = 0;
  uint32_t sessionStartedMs = 0;
  uint32_t sessionPackets = 0;
  bool sessionOpen = false;
  uint8_t connectFailStreak = 0;
  bool wifiRadioTuned = false;

  for (;;) {
    uint32_t mutexWaitMs = 0;

    if (WiFi.status() != WL_CONNECTED) {
      wifiRadioTuned = false;
      tcpLinkHealthy = false;
      rpiHostIpValid = false;
      if (sessionOpen) {
        logTcpDrop("wifi_down", sessionStartedMs, sessionPackets,
                   client.available());
        sessionOpen = false;
      }
      if (client.connected() && beginTcpCritical(mutexWaitMs)) {
        client.stop();
        endTcpCritical();
      }
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }

    if (!wifiRadioTuned) {
      WiFi.setSleep(false);
      WiFi.setTxPower(WIFI_POWER_19_5dBm);
      wifiRadioTuned = true;
      Serial.printf("[wifi] radio tuned tx=19.5dBm rssi=%d ip=%s\n",
                    WiFi.RSSI(), WiFi.localIP().toString().c_str());
    }

    if (!client.connected()) {
      // Stand the thermal transmitter down for the whole reconnect, not just
      // for the connect() call: the radio has to be quiet for the handshake to
      // complete, and TCP_CRITICAL_BIT only covers the moments this task holds
      // the mutex.
      tcpLinkHealthy = false;
      // Arriving here with sessionOpen still set means the peer closed or reset
      // the socket while this node believed the link was healthy. That is the
      // signature of a Pi-side receive timeout rather than a local send error,
      // so it is counted separately from write_failed.
      if (sessionOpen) {
        tcpPeerClosed = tcpPeerClosed + 1;
        logTcpDrop("peer_closed", sessionStartedMs, sessionPackets,
                   client.available());
        sessionOpen = false;
      }
      Serial.printf("[network] connecting to %s:%u\n", RPI_HOST, RPI_PORT);
      bool connected = false;
      if (beginTcpCritical(mutexWaitMs)) {
        const uint32_t connectStartedMs = millis();
        errno = 0;
        connected = client.connect(RPI_HOST, RPI_PORT, TCP_CONNECT_TIMEOUT_MS);
        lastConnectDurationMs = millis() - connectStartedMs;
        if (!connected) {
          tcpLastErrno = errno;
          client.stop();
        }
        endTcpCritical();
      } else {
        tcpMutexTimeouts = tcpMutexTimeouts + 1;
      }
      if (!connected) {
        tcpConnectionFailures = tcpConnectionFailures + 1;
        connectFailStreak = connectFailStreak + 1;
        if (tcpErrorLogDue()) {
          Serial.printf(
              "[tcp-connect-fail] mutex_wait_ms=%lu connect_ms=%lu errno=%ld "
              "failures=%lu streak=%u\n",
              static_cast<unsigned long>(mutexWaitMs),
              static_cast<unsigned long>(lastConnectDurationMs),
              static_cast<long>(tcpLastErrno),
              static_cast<unsigned long>(tcpConnectionFailures),
              static_cast<unsigned>(connectFailStreak));
        }
        if (connectFailStreak >= TCP_CONNECT_FAILS_BEFORE_REASSOC) {
          Serial.println("[wifi] reassociate after tcp connect streak");
          WiFi.reconnect();
          wifiRadioTuned = false;
          connectFailStreak = 0;
          vTaskDelay(pdMS_TO_TICKS(4000));
        } else {
          vTaskDelay(pdMS_TO_TICKS(TCP_CONNECT_FAIL_DELAY_MS));
        }
        continue;
      }
      connectFailStreak = 0;
      client.setNoDelay(true);
      sessionStartedMs = millis();
      sessionPackets = 0;
      sessionOpen = true;
      // Leave tcpLinkHealthy false until the first telemetry write succeeds
      // so the thermal sender cannot refill the Wi-Fi TX queue during the
      // window between SYN-ACK and the first JSON packet.
      tcpSessions = tcpSessions + 1;
      Serial.printf(
          "[tcp-open] session=%lu connect_ms=%lu mutex_wait_ms=%lu rssi=%d "
          "heap=%lu udp_held=1\n",
          static_cast<unsigned long>(tcpSessions),
          static_cast<unsigned long>(lastConnectDurationMs),
          static_cast<unsigned long>(mutexWaitMs),
          static_cast<int>(WiFi.RSSI()),
          static_cast<unsigned long>(ESP.getFreeHeap()));
    }

    // Scalar telemetry has priority and is small.
    if (xQueueReceive(telemetryQueue, &telemetry, 0) == pdTRUE) {
      telemetryQueueOverwrites = telemetryQueueOverwrites +
                                 telemetry.sequence - lastDequeuedSequence - 1;
      lastDequeuedSequence = telemetry.sequence;
      // The producer stamps uptimeMs from the same millis() clock on core 1,
      // so this difference is how long the snapshot waited for the network
      // task. It separates "loop() published late" from "the socket was slow".
      lastQueueWaitMs =
          static_cast<uint32_t>(millis() - telemetry.uptimeMs);

      size_t jsonPayloadLength = 0;
      bool sent = false;
      // Distinguishes "the packet never reached the socket" from "the socket
      // is gone". A mutex timeout is the first: it leaves the connection fully
      // intact, so none of the teardown below may run for it. Only the write
      // path calls client.stop(), and only that ends the session.
      bool socketClosedByFailure = false;
      bool writeAttempted = false;
      uint32_t writeDurationMs = 0;
      TcpWriteReport report{};
      if (beginTcpCritical(mutexWaitMs)) {
        writeAttempted = true;
        const uint32_t writeStartedMs = millis();
        sent = sendTelemetry(client, telemetry, jsonPayloadLength, report);
        writeDurationMs = millis() - writeStartedMs;
        if (!sent) {
          tcpLastErrno = report.lastErrno;
          // A 0-byte fail-fast timeout never reached the Pi, so keep the
          // session. Any partial SNST header/body would desynchronize the
          // receiver, so that path still closes.
          if (report.bytesWritten > 0 || !report.connectedAtEnd) {
            client.stop();
            socketClosedByFailure = true;
          }
        }
        endTcpCritical();
        if (writeDurationMs > tcpWriteMaxMs) tcpWriteMaxMs = writeDurationMs;
        if (writeDurationMs >= TCP_WRITE_WARN_MS) {
          tcpSlowWrites = tcpSlowWrites + 1;
          requestUdpHoldoff(millis(), UDP_SLOW_TCP_HOLDOFF_MS);
        }
        if (report.zeroWrites > 0) tcpWriteStalls = tcpWriteStalls + 1;
        if (report.partialWrites > 0) tcpPartialWrites = tcpPartialWrites + 1;
      } else {
        tcpMutexTimeouts = tcpMutexTimeouts + 1;
        if (tcpErrorLogDue()) {
          Serial.printf(
              "[tcp-blocked] seq=%lu mutex_wait_ms=%lu timeouts=%lu -- the "
              "thermal UDP task held the TX mutex past the limit, so this "
              "telemetry packet never reached the socket\n",
              static_cast<unsigned long>(telemetry.sequence),
              static_cast<unsigned long>(mutexWaitMs),
              static_cast<unsigned long>(tcpMutexTimeouts));
        }
      }
      lastMutexWaitMs = mutexWaitMs;

      if (!sent) {
        // Counted either way: this snapshot did not reach the Pi. Which of the
        // two causes it was is already on the console -- [tcp-send-fail] here,
        // or [tcp-blocked] / [tcp-send-timeout] above.
        tcpSendFailures = tcpSendFailures + 1;
        requestUdpHoldoff(millis(), UDP_SLOW_TCP_HOLDOFF_MS);
        if (!socketClosedByFailure && writeAttempted) {
          tcpWriteTimeouts = tcpWriteTimeouts + 1;
          if (tcpErrorLogDue()) {
            Serial.printf(
                "[tcp-send-timeout] seq=%lu wrote=%u/%u elapsed_ms=%lu "
                "stall_ms=%lu zero_writes=%u errno=%ld kept_open=1 "
                "timeouts=%lu -- skipped this snapshot instead of blocking "
                "the radio past the Pi deadline\n",
                static_cast<unsigned long>(telemetry.sequence),
                static_cast<unsigned>(report.bytesWritten),
                static_cast<unsigned>(PACKET_HEADER_SIZE + jsonPayloadLength),
                static_cast<unsigned long>(report.elapsedMs),
                static_cast<unsigned long>(report.longestStallMs),
                static_cast<unsigned>(report.zeroWrites),
                static_cast<long>(report.lastErrno),
                static_cast<unsigned long>(tcpWriteTimeouts));
          }
        }
      }

      if (socketClosedByFailure) {
        // client.stop() ran above, so the socket is gone. Drop the signal here
        // rather than waiting for the next loop to notice, so the thermal task
        // is quiet before the reconnect starts. tcpLinkHealthy is set true only
        // after a successful write, not after connect().
        tcpLinkHealthy = false;
        if (tcpErrorLogDue()) {
          Serial.printf(
              "[tcp-send-fail] seq=%lu wrote=%u/%u elapsed_ms=%lu stall_ms=%lu "
              "zero_writes=%u partial=%u errno=%ld connected=%d failures=%lu\n",
              static_cast<unsigned long>(telemetry.sequence),
              static_cast<unsigned>(report.bytesWritten),
              static_cast<unsigned>(PACKET_HEADER_SIZE + jsonPayloadLength),
              static_cast<unsigned long>(report.elapsedMs),
              static_cast<unsigned long>(report.longestStallMs),
              static_cast<unsigned>(report.zeroWrites),
              static_cast<unsigned>(report.partialWrites),
              static_cast<long>(report.lastErrno),
              report.connectedAtEnd ? 1 : 0,
              static_cast<unsigned long>(tcpSendFailures));
        }
        if (sessionOpen) {
          logTcpDrop("write_failed", sessionStartedMs, sessionPackets, 0);
          sessionOpen = false;
        }
        vTaskDelay(pdMS_TO_TICKS(TCP_RECONNECT_DELAY_MS));
      }

      if (sent) {
        tcpLinkHealthy = true;
        // On-wire cadence as the Pi experiences it. The receiver closes the
        // socket when a packet does not complete inside its own timeout, so
        // this gap -- not the send-failure count -- is the quantity that
        // predicts a disconnect. It is measured after the write completes,
        // which is the moment the last byte was handed to lwIP.
        const uint32_t completedMs = millis();
        if (lastTelemetrySentMs != 0) {
          const uint32_t gapMs =
              static_cast<uint32_t>(completedMs - lastTelemetrySentMs);
          if (gapMs > telemetryGapMaxMs) telemetryGapMaxMs = gapMs;
          if (gapMs >= PI_PACKET_DEADLINE_MS) {
            telemetryGapOverDeadline = telemetryGapOverDeadline + 1;
          }
          if (gapMs >= TELEMETRY_GAP_WARN_MS) {
            telemetryGapOverWarn = telemetryGapOverWarn + 1;
            Serial.printf(
                "[tcp-gap] seq=%lu gap_ms=%lu pi_deadline_ms=%lu "
                "queue_wait_ms=%lu mutex_wait_ms=%lu write_ms=%lu "
                "stall_ms=%lu zero_writes=%u overwrites=%lu udp_on=%d "
                "udp_dg_max_ms=%lu rssi=%d%s\n",
                static_cast<unsigned long>(telemetry.sequence),
                static_cast<unsigned long>(gapMs),
                static_cast<unsigned long>(PI_PACKET_DEADLINE_MS),
                static_cast<unsigned long>(lastQueueWaitMs),
                static_cast<unsigned long>(mutexWaitMs),
                static_cast<unsigned long>(writeDurationMs),
                static_cast<unsigned long>(report.longestStallMs),
                static_cast<unsigned>(report.zeroWrites),
                static_cast<unsigned long>(telemetryQueueOverwrites),
                thermalUdpEnabled ? 1 : 0,
                static_cast<unsigned long>(udpDatagramMaxMs),
                static_cast<int>(WiFi.RSSI()),
                gapMs >= PI_PACKET_DEADLINE_MS ? "  <<< EXCEEDS_PI_DEADLINE"
                                               : "");
          }
        }
        lastTelemetrySentMs = completedMs;
        telemetryPacketsSent = telemetryPacketsSent + 1;
        ++sessionPackets;
      }

      const uint32_t now = millis();
      if (lastNetworkLogMs == 0 ||
          static_cast<uint32_t>(now - lastNetworkLogMs) >=
              HEALTH_LOG_PERIOD_MS) {
        Serial.printf(
            "[network] tcp connect_ms=%lu write_ms=%lu queue_wait_ms=%lu "
            "mutex_wait_ms=%lu seq=%lu json_bytes=%u gap_max_ms=%lu "
            "tcp_send_failures=%lu thermal_preemptions=%lu "
            "thermal_udp_failures=%lu\n",
            static_cast<unsigned long>(lastConnectDurationMs),
            static_cast<unsigned long>(writeDurationMs),
            static_cast<unsigned long>(lastQueueWaitMs),
            static_cast<unsigned long>(lastMutexWaitMs),
            static_cast<unsigned long>(telemetry.sequence),
            static_cast<unsigned>(jsonPayloadLength),
            static_cast<unsigned long>(telemetryGapMaxMs),
            static_cast<unsigned long>(tcpSendFailures),
            static_cast<unsigned long>(thermalFramesPreempted),
            static_cast<unsigned long>(thermalUdpSendFailures));
        lastNetworkLogMs = now;
      }

      if (!sent) {
        continue;
      }
    }

    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void thermalUdpTask(void *parameter) {
  (void)parameter;
  WiFiUDP udp;
  bool udpStarted = false;
  uint32_t lastDequeuedSequence = 0;
  uint32_t consecutiveHardFailures = 0;
  uint32_t enomemBackoffMs = UDP_ENOMEM_BACKOFF_MIN_MS;
  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      rpiHostIpValid = false;
      if (udpStarted) {
        udp.stop();
        udpStarted = false;
      }
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    if (!ensureRpiHostIp()) {
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    if (!udpStarted) {
      udpStarted = udp.begin(0);
      if (!udpStarted) {
        vTaskDelay(pdMS_TO_TICKS(250));
        continue;
      }
    }

    const uint32_t now = millis();
    if (ESP.getFreeHeap() < HEAP_RECLAIM_LOW_WATER_BYTES) {
      requestUdpHoldoff(now, enomemBackoffMs);
    }

    if (xQueueReceive(thermalQueue, &thermalNetworkFrame, 0) == pdTRUE) {
      thermalQueueOverwrites = thermalQueueOverwrites +
                               thermalNetworkFrame.frameSequence -
                               lastDequeuedSequence - 1;
      lastDequeuedSequence = thermalNetworkFrame.frameSequence;
      // Wall time for the whole 9-datagram frame. Compared against the 1000 ms
      // budget of a 1 fps stream (THERMAL_FRAME_RATE_DIVIDER = 25), this
      // says whether the transmitter is keeping up or saturating the radio.
      const uint32_t frameStartedMs = millis();
      const ThermalSendResult result =
          sendThermalUdp(udp, thermalNetworkFrame);
      const uint32_t frameMs =
          static_cast<uint32_t>(millis() - frameStartedMs);
      if (frameMs > udpFrameMaxMs) udpFrameMaxMs = frameMs;
      switch (result) {
        case ThermalSendResult::Sent:
          thermalUdpFramesSent = thermalUdpFramesSent + 1;
          consecutiveHardFailures = 0;
          enomemBackoffMs = UDP_ENOMEM_BACKOFF_MIN_MS;
          udpBackoffMs = 0;
          break;
        case ThermalSendResult::Preempted:
          thermalFramesPreempted = thermalFramesPreempted + 1;
          break;
        case ThermalSendResult::Failed: {
          thermalUdpSendFailures = thermalUdpSendFailures + 1;
          const int err = static_cast<int>(udpLastErrno);
          if (thermalUdpSendFailures <= 3 ||
              thermalUdpSendFailures % 25 == 0) {
            Serial.printf(
                "[udp-fail] frame=%lu frame_ms=%lu errno=%ld failures=%lu "
                "consecutive=%lu\n",
                static_cast<unsigned long>(
                    thermalNetworkFrame.frameSequence),
                static_cast<unsigned long>(frameMs),
                static_cast<long>(udpLastErrno),
                static_cast<unsigned long>(thermalUdpSendFailures),
                static_cast<unsigned long>(consecutiveHardFailures + 1));
          }
          if (err == ENOMEM || err == ENOBUFS || isTransientSendErrno(err)) {
            // lwIP is out of TX pbufs. Rebuilding the socket does not free
            // buffers already queued in the Wi-Fi driver and the malloc of a
            // new 1460-byte TX buffer fragments the heap. Pause instead.
            consecutiveHardFailures = 0;
            requestUdpHoldoff(millis(), enomemBackoffMs);
            udpBackoffMs = enomemBackoffMs;
            static uint32_t lastBackoffLogMs = 0;
            const uint32_t backoffNow = millis();
            if (lastBackoffLogMs == 0 ||
                static_cast<uint32_t>(backoffNow - lastBackoffLogMs) >=
                    1000) {
              lastBackoffLogMs = backoffNow;
              Serial.printf(
                  "[udp-backoff] errno=%ld backoff_ms=%lu heap=%lu "
                  "max_alloc=%lu holdoff=%lu\n",
                  static_cast<long>(err),
                  static_cast<unsigned long>(enomemBackoffMs),
                  static_cast<unsigned long>(ESP.getFreeHeap()),
                  static_cast<unsigned long>(ESP.getMaxAllocHeap()),
                  static_cast<unsigned long>(udpHoldoffEvents));
            }
            if (enomemBackoffMs < UDP_ENOMEM_BACKOFF_MAX_MS) {
              enomemBackoffMs = enomemBackoffMs * 2;
              if (enomemBackoffMs > UDP_ENOMEM_BACKOFF_MAX_MS) {
                enomemBackoffMs = UDP_ENOMEM_BACKOFF_MAX_MS;
              }
            }
          } else {
            ++consecutiveHardFailures;
            if (udpErrorLooksLikeDeadSocket(err) ||
                consecutiveHardFailures >= UDP_MAX_CONSECUTIVE_FAILURES) {
              if (reclaimUdpHeap(udp, udpStarted)) {
                consecutiveHardFailures = 0;
                udpSocketRestarts = udpSocketRestarts + 1;
                Serial.printf(
                    "[udp-restart] socket rebuilt after hard failure "
                    "errno=%ld restarts=%lu\n",
                    static_cast<long>(udpLastErrno),
                    static_cast<unsigned long>(udpSocketRestarts));
              }
            }
          }
          break;
        }
        case ThermalSendResult::Suppressed:
          thermalFramesSuppressed = thermalFramesSuppressed + 1;
          break;
        case ThermalSendResult::Deferred:
          thermalFramesDeferred = thermalFramesDeferred + 1;
          break;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// Grouped subsystem lines rather than one 30-field, ~470-character line. Every
// field the single-line form carried is still here, under the subsystem that
// owns it, plus the three that were missing when it mattered most: the node's
// own IP, the RSSI, and the minimum free heap.
void logHealth(uint32_t now) {
  if (!scheduleDue(now, lastHealthLogMs, HEALTH_LOG_PERIOD_MS)) return;

  const bool wifiUp = WiFi.status() == WL_CONNECTED;
  const IPAddress ip = WiFi.localIP();

  Serial.printf("[health] ===== up=%lus =====\n",
                static_cast<unsigned long>(now / 1000));

  // localIP() answers what the old line could not: whether this node is even on
  // the subnet RPI_HOST lives on. Without it a wrong-subnet run reads exactly
  // like a dead Pi -- repeated "connecting to", no other evidence either way.
  Serial.printf(
      "[health] link    wifi=%s ip=%u.%u.%u.%u rssi=%d rpi=%s:%u tcp=%s\n",
      wifiUp ? "up" : "down", static_cast<unsigned>(ip[0]),
      static_cast<unsigned>(ip[1]), static_cast<unsigned>(ip[2]),
      static_cast<unsigned>(ip[3]),
      wifiUp ? static_cast<int>(WiFi.RSSI()) : 0, RPI_HOST,
      static_cast<unsigned>(RPI_PORT), tcpLinkHealthy ? "up" : "down");

  Serial.printf(
      "[health] tcp     conn_fail=%lu send_fail=%lu write_to=%lu queue_ovw=%lu\n",
      static_cast<unsigned long>(tcpConnectionFailures),
      static_cast<unsigned long>(tcpSendFailures),
      static_cast<unsigned long>(tcpWriteTimeouts),
      static_cast<unsigned long>(telemetryQueueOverwrites));

  Serial.printf(
      "[health] thermal started=%d frames=%lu udp_sent=%lu udp_fail=%lu "
      "preempt=%lu "
      "yield=%lu defer=%lu holdoff=%lu restarts=%lu crc_err=%lu rng_err=%lu "
      "status_fail=%lu queue_ovw=%lu\n",
      thermalStarted ? 1 : 0,
      static_cast<unsigned long>(thermalSequence),
      static_cast<unsigned long>(thermalUdpFramesSent),
      static_cast<unsigned long>(thermalUdpSendFailures),
      static_cast<unsigned long>(thermalFramesPreempted),
      static_cast<unsigned long>(thermalTcpYields),
      static_cast<unsigned long>(thermalFramesDeferred),
      static_cast<unsigned long>(udpHoldoffEvents),
      static_cast<unsigned long>(udpSocketRestarts),
      static_cast<unsigned long>(thermalCrcErrors),
      static_cast<unsigned long>(thermalRangeErrors),
      static_cast<unsigned long>(thermalStatusQueryFailures),
      static_cast<unsigned long>(thermalQueueOverwrites));

  char respText[16];
  char heartText[16];
  formatNullableFloat(respText, sizeof(respText),
                      isFresh(lastRespirationMs, now, MMWAVE_STALE_MS),
                      respirationRate);
  formatNullableFloat(heartText, sizeof(heartText),
                      isFresh(lastHeartMs, now, MMWAVE_STALE_MS), heartRate);
  Serial.printf(
      "[health] sensors resp=%s heart=%s co2=%u pir=%d co2_age_ms=%ld "
      "resp_age_ms=%ld heart_age_ms=%ld presence_age_ms=%ld "
      "phase=%.4f phase_age_ms=%ld phase_seq=%lu "
      "mmw_ok=%lu mmw_age_ms=%ld mmw_uart=%u mmw_miss=%lu\n",
      respText, heartText, static_cast<unsigned>(co2Ppm),
      pirMotion ? 1 : 0,
      lastCo2Ms == 0 ? -1L : static_cast<long>(now - lastCo2Ms),
      lastRespirationMs == 0 ? -1L
                             : static_cast<long>(now - lastRespirationMs),
      lastHeartMs == 0 ? -1L : static_cast<long>(now - lastHeartMs),
      lastPresenceMs == 0 ? -1L : static_cast<long>(now - lastPresenceMs),
      breathPhase,
      lastPhaseMs == 0 ? -1L : static_cast<long>(now - lastPhaseMs),
      static_cast<unsigned long>(phaseSequence),
      static_cast<unsigned long>(mmWaveUpdateSuccesses),
      lastMmWaveUpdateMs == 0
          ? -1L
          : static_cast<long>(now - lastMmWaveUpdateMs),
      static_cast<unsigned>(mmWaveSerial.available()),
      static_cast<unsigned long>(mmWaveUpdateMisses));

  Serial.printf(
      "[health] co2     model=MH-Z19B identity=INFERRED_UART_SAMPLE "
      "preheat=%d preheat_complete=%d uart_ppm=%u event_id=%lu "
      "accepted=%lu csum_fail=%lu "
      "timeout=%lu short=%lu txn=%s uart1=%u\n",
      co2InPreheat(now) ? 1 : 0, co2InPreheat(now) ? 0 : 1,
      static_cast<unsigned>(co2LastUartPpm),
      static_cast<unsigned long>(co2MeasurementEventId),
      static_cast<unsigned long>(co2AcceptedSamples),
      static_cast<unsigned long>(co2ChecksumFailures),
      static_cast<unsigned long>(co2TimeoutFailures),
      static_cast<unsigned long>(co2ShortFrameFailures),
      mhz19Txn == Mhz19Txn::AWAIT_RESPONSE ? "AWAIT" : "IDLE",
      static_cast<unsigned>(mhz19Serial.available()));

  // min_heap is the one that matters here: the transmit-path collapse showed up
  // as a ~40 KB dip that the instantaneous value had already recovered from by
  // the time the next line printed.
  Serial.printf(
      "[health] sys     heap=%lu min_heap=%lu max_alloc=%lu reclaim=%lu "
      "reclaim_skip_tcp=%lu holdoff=%d backoff_ms=%lu write_to=%lu\n",
      static_cast<unsigned long>(ESP.getFreeHeap()),
      static_cast<unsigned long>(ESP.getMinFreeHeap()),
      static_cast<unsigned long>(ESP.getMaxAllocHeap()),
      static_cast<unsigned long>(heapReclaims),
      static_cast<unsigned long>(heapReclaimSkippedTcp),
      udpHoldoffActive(now) ? 1 : 0,
      static_cast<unsigned long>(udpBackoffMs),
      static_cast<unsigned long>(tcpWriteTimeouts));
}

// Absolute counters answer "did it ever happen"; rates answer "is it happening
// now". Only the second question separates a healthy 2 fps thermal stream from
// a READY line stuck high that captures and transmits thousands of times per
// second.
void logLinkDiagnostics(uint32_t now) {
  static uint32_t lastSampleMs = 0;
  static uint32_t lastLoops = 0;
  static uint32_t lastAttempts = 0;
  static uint32_t lastFrames = 0;
  static uint32_t lastDatagrams = 0;
  static uint32_t lastBytes = 0;

  // scheduleDue() runs unconditionally so the window baselines below stay
  // anchored to a real interval even while the periodic copy is silenced.
  const bool due = scheduleDue(now, lastLinkLogMs, DIAG_LOG_PERIOD_MS);
  if (!linkDiagnosticsOnce && !(due && linkDiagnosticsEnabled)) return;
  linkDiagnosticsOnce = false;

  const uint32_t windowMs =
      lastSampleMs == 0 ? DIAG_LOG_PERIOD_MS
                        : static_cast<uint32_t>(now - lastSampleMs);
  const uint32_t divisor = windowMs == 0 ? 1 : windowMs;
  const uint32_t datagrams = udpDatagramsSent;
  const uint32_t bytes = udpBytesSent;

  Serial.printf(
      "[link] up_s=%lu loop_hz=%lu ready_pin=%lu ready_i2c=%lu cap_hz=%lu "
      "frame_hz=%lu cap_max_ms=%lu crc_err=%lu rng_err=%lu udp_on=%d "
      "cap_on=%d udp_hz=%lu udp_kbps=%lu udp_dg_max_ms=%lu "
      "udp_frame_max_ms=%lu udp_slow=%lu udp_fail=%lu preempt=%lu "
      "suppress=%lu defer=%lu udp_restarts=%lu "
      "tcp_sess=%lu drops=%lu peer_closed=%lu sent=%lu "
      "gap_max_ms=%lu gap_warn=%lu gap_late=%lu write_max_ms=%lu slow_w=%lu "
      "stalls=%lu partial=%lu mutex_max_ms=%lu mutex_to=%lu tcp_errno=%ld "
      "udp_errno=%ld rssi=%d heap=%lu min_heap=%lu max_alloc=%lu "
      "holdoff=%d backoff_ms=%lu write_to=%lu\n",
      static_cast<unsigned long>(now / 1000),
      static_cast<unsigned long>((loopIterations - lastLoops) * 1000UL /
                                 divisor),
      static_cast<unsigned long>(thermalReadyByPin),
      static_cast<unsigned long>(thermalReadyByI2c),
      static_cast<unsigned long>(
          (thermalCaptureAttempts - lastAttempts) * 1000UL / divisor),
      static_cast<unsigned long>((thermalSequence - lastFrames) * 1000UL /
                                 divisor),
      static_cast<unsigned long>(thermalCaptureMaxMs),
      static_cast<unsigned long>(thermalCrcErrors),
      static_cast<unsigned long>(thermalRangeErrors),
      thermalUdpEnabled ? 1 : 0,
      thermalCaptureEnabled ? 1 : 0,
      static_cast<unsigned long>((datagrams - lastDatagrams) * 1000UL /
                                 divisor),
      static_cast<unsigned long>((bytes - lastBytes) * 8UL / divisor),
      static_cast<unsigned long>(udpDatagramMaxMs),
      static_cast<unsigned long>(udpFrameMaxMs),
      static_cast<unsigned long>(udpSlowDatagrams),
      static_cast<unsigned long>(thermalUdpSendFailures),
      static_cast<unsigned long>(thermalFramesPreempted),
      static_cast<unsigned long>(thermalFramesSuppressed),
      static_cast<unsigned long>(thermalFramesDeferred),
      static_cast<unsigned long>(udpSocketRestarts),
      static_cast<unsigned long>(tcpSessions),
      static_cast<unsigned long>(tcpDrops),
      static_cast<unsigned long>(tcpPeerClosed),
      static_cast<unsigned long>(telemetryPacketsSent),
      static_cast<unsigned long>(telemetryGapMaxMs),
      static_cast<unsigned long>(telemetryGapOverWarn),
      static_cast<unsigned long>(telemetryGapOverDeadline),
      static_cast<unsigned long>(tcpWriteMaxMs),
      static_cast<unsigned long>(tcpSlowWrites),
      static_cast<unsigned long>(tcpWriteStalls),
      static_cast<unsigned long>(tcpPartialWrites),
      static_cast<unsigned long>(tcpMutexWaitMaxMs),
      static_cast<unsigned long>(tcpMutexTimeouts),
      static_cast<long>(tcpLastErrno),
      static_cast<long>(udpLastErrno),
      static_cast<int>(WiFi.RSSI()),
      static_cast<unsigned long>(ESP.getFreeHeap()),
      static_cast<unsigned long>(ESP.getMinFreeHeap()),
      static_cast<unsigned long>(ESP.getMaxAllocHeap()),
      udpHoldoffActive(now) ? 1 : 0,
      static_cast<unsigned long>(udpBackoffMs),
      static_cast<unsigned long>(tcpWriteTimeouts));

  lastSampleMs = now;
  lastLoops = loopIterations;
  lastAttempts = thermalCaptureAttempts;
  lastFrames = thermalSequence;
  lastDatagrams = datagrams;
  lastBytes = bytes;
}

// Only the peak gauges and diagnostic tallies are cleared. Wire-visible state
// -- telemetrySequence, thermalSequence -- is left alone, because the Pi
// rejects a sequence that moves backwards inside one connection.
void resetDiagnosticCounters() {
  tcpSessions = 0;
  tcpDrops = 0;
  tcpShortSessions = 0;
  tcpPeerClosed = 0;
  tcpWriteStalls = 0;
  tcpPartialWrites = 0;
  tcpMutexTimeouts = 0;
  tcpMutexWaitMaxMs = 0;
  tcpMutexWaitSlow = 0;
  tcpWriteMaxMs = 0;
  tcpSlowWrites = 0;
  telemetryGapMaxMs = 0;
  telemetryGapOverWarn = 0;
  telemetryGapOverDeadline = 0;
  telemetryPacketsSent = 0;
  udpDatagramsSent = 0;
  udpBytesSent = 0;
  udpDatagramMaxMs = 0;
  udpSlowDatagrams = 0;
  udpFrameMaxMs = 0;
  thermalFramesSuppressed = 0;
  thermalFramesDeferred = 0;
  thermalTcpYields = 0;
  heapReclaims = 0;
  heapReclaimSkippedTcp = 0;
  udpHoldoffEvents = 0;
  udpHoldoffUntilMs = 0;
  udpBackoffMs = 0;
  tcpWriteTimeouts = 0;
  udpSocketRestarts = 0;
  tcpLastErrno = 0;
  udpLastErrno = 0;
  thermalCaptureAttempts = 0;
  thermalReadyByPin = 0;
  thermalReadyByI2c = 0;
  thermalCaptureMaxMs = 0;
}

void printDiagnosticHelp() {
  Serial.println(
      "[help] serial commands: u = toggle thermal UDP transmit, "
      "c = toggle thermal capture, t = debug force thermal re-init, "
      "s = print [link] now, "
      "l = toggle periodic [link], r = reset diagnostic counters, "
      "h = this help");
  Serial.printf(
      "[help] pi receive deadlines: LCD/runtime frame=5000 ms; this node "
      "warns at gap_ms>=%lu, skips a TCP snapshot after %lu ms with 0 "
      "bytes sent, and closes only if a partial packet exceeds %lu ms\n",
      static_cast<unsigned long>(TELEMETRY_GAP_WARN_MS),
      static_cast<unsigned long>(TCP_WRITE_SKIP_MS),
      static_cast<unsigned long>(TCP_WRITE_DEADLINE_MS));
  Serial.println(
      "[help] thermal UDP/capture start ON. Camera re-inits itself if I2C "
      "appears or capture stalls; serial t is not required. "
      "Monitor with dtr=off,rts=off or the board resets.");
}

// Non-blocking, so the delay-free runtime contract of loop() still holds.
void handleSerialCommand() {
  while (Serial.available() > 0) {
    switch (Serial.read()) {
      case 'u':
      case 'U':
        thermalUdpEnabled = !thermalUdpEnabled;
        Serial.printf("[cmd] thermal UDP transmit %s cap_on=%d udp_on=%d\n",
                      thermalUdpEnabled ? "ENABLED" : "DISABLED",
                      thermalCaptureEnabled ? 1 : 0,
                      thermalUdpEnabled ? 1 : 0);
        break;
      case 'c':
      case 'C':
        thermalCaptureEnabled = !thermalCaptureEnabled;
        Serial.printf("[cmd] thermal capture %s cap_on=%d udp_on=%d\n",
                      thermalCaptureEnabled ? "ENABLED" : "DISABLED",
                      thermalCaptureEnabled ? 1 : 0,
                      thermalUdpEnabled ? 1 : 0);
        break;
      case 't':
      case 'T':
        Serial.println("[cmd] thermal camera re-init");
        initializeThermalCamera();
        Serial.printf("[cmd] thermal started=%d addr=0x%02X cap_on=%d udp_on=%d\n",
                      thermalStarted ? 1 : 0,
                      static_cast<unsigned>(thermalAddress),
                      thermalCaptureEnabled ? 1 : 0,
                      thermalUdpEnabled ? 1 : 0);
        break;
      case 'r':
      case 'R':
        resetDiagnosticCounters();
        Serial.println("[cmd] diagnostic counters cleared");
        break;
      case 's':
      case 'S':
        // One-shot, independent of the periodic gate, so 's' still works while
        // the periodic copy is silenced.
        linkDiagnosticsOnce = true;
        break;
      case 'l':
      case 'L':
        linkDiagnosticsEnabled = !linkDiagnosticsEnabled;
        Serial.printf("[cmd] periodic [link] logging %s\n",
                      linkDiagnosticsEnabled ? "ENABLED" : "DISABLED");
        break;
      case 'h':
      case 'H':
      case '?':
        printDiagnosticHelp();
        break;
      default:
        break;  // Ignore newlines and stray bytes from the terminal.
    }
  }
}

void setup() {
  Serial.begin(USB_BAUD);
  setupWait(500);
  Serial.println("\nSafeNest ESP32 sensor node starting (MH-Z19B CO2 v2 port)");
  initializeBootId();
  Serial.printf("[identity] device=%s boot=%s firmware=%s diag=%s reset=%d\n",
                DEVICE_ID, bootId, NODE_FIRMWARE_VERSION,
                DIAGNOSTIC_BUILD_ID, static_cast<int>(esp_reset_reason()));
  printDiagnosticHelp();

  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_THERMAL_CS, OUTPUT);
  digitalWrite(PIN_THERMAL_CS, HIGH);
  pinMode(PIN_THERMAL_READY, INPUT);
  pinMode(PIN_THERMAL_RESET, OUTPUT);
  digitalWrite(PIN_THERMAL_RESET, HIGH);

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  Wire.setClock(100000);
  thermalSpi.begin(PIN_THERMAL_SCLK, PIN_THERMAL_MISO,
                   PIN_THERMAL_MOSI, PIN_THERMAL_CS);

  // setPins() before the library's begin() makes its pin-less Serial.begin()
  // retain GPIO16/17 on both Arduino-ESP32 core 2.x and 3.x.
  mmWaveSerial.setPins(PIN_MMWAVE_RX, PIN_MMWAVE_TX);
  mmWave.begin(&mmWaveSerial, MMWAVE_BAUD, 0);
  mmWaveSerial.setTimeout(0);
  Serial.printf(
      "[mmw] UART2 RX=%d TX=%d baud=%lu parser=local sof=0x01 "
      "frame_stale_ms=%lu thermal_udp_default=1\n",
      PIN_MMWAVE_RX, PIN_MMWAVE_TX, static_cast<unsigned long>(MMWAVE_BAUD),
      static_cast<unsigned long>(MMWAVE_FRAME_STALE_MS));

  // UART1 for MH-Z19B. setPins() before begin() so core 2.x/3.x keep 32/33
  // instead of the WROOM flash pins 9/10. 9600 8N1 TTL 3.3 V; TX/RX crossed.
  mhz19Serial.setPins(PIN_MHZ19_RX, PIN_MHZ19_TX);
  mhz19Serial.begin(MHZ19_BAUD);
  mhz19Serial.setTimeout(0);
  Serial.printf(
      "[co2] MH-Z19B UART1 RX=%d TX=%d baud=%lu sample_period_ms=%lu "
      "preheat_ms=%lu ABC=factory-default-ON range_cmd=not-sent "
      "identity=%s\n",
      PIN_MHZ19_RX, PIN_MHZ19_TX, static_cast<unsigned long>(MHZ19_BAUD),
      static_cast<unsigned long>(CO2_UART_SAMPLE_PERIOD_MS),
      static_cast<unsigned long>(CO2_PREHEAT_MS), CO2_EVENT_IDENTITY_CLASS);

  telemetryQueue = xQueueCreate(1, sizeof(TelemetrySnapshot));
  thermalQueue = xQueueCreate(1, sizeof(ThermalTxFrame));
  networkEvents = xEventGroupCreate();
  networkTxMutex = xSemaphoreCreateMutex();
  if (telemetryQueue == nullptr || thermalQueue == nullptr ||
      networkEvents == nullptr || networkTxMutex == nullptr) {
    Serial.println("[fatal] queue or network synchronization allocation failed");
    while (true) vTaskDelay(portMAX_DELAY);
  }

  initializeThermalCamera();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency for the continuous thermal stream
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  Serial.printf("[wifi] connecting to %s asynchronously tx=19.5dBm\n", WIFI_SSID);

  // Priority alone cannot preempt datagrams already queued by lwIP, so the
  // per-datagram mutex and TCP critical bit remain required for bounded access.
  xTaskCreatePinnedToCore(telemetryTcpTask, "telemetry-tcp", 8192, nullptr, 2,
                          nullptr, 0);
  xTaskCreatePinnedToCore(thermalUdpTask, "thermal-udp", 8192, nullptr, 1,
                          nullptr, 0);
}

void loop() {
  const uint32_t now = millis();
  ++loopIterations;

  pollMmWave(now);
  pollCo2(now);
  maybeRecoverThermalCamera(now);
  captureThermalIfReady(now);
  // SPI capture blocks UART servicing. Drain again so a mid-frame SOF does
  // not sit in the Seeed assembler until the next ~1 fps camera tick.
  pollMmWave(millis());

  if (scheduleDue(now, lastPirPollMs, PIR_PERIOD_MS)) {
    pirMotion = digitalRead(PIN_PIR) == HIGH;
  }

  publishTelemetrySnapshot(now);
  handleSerialCommand();
  logLinkDiagnostics(now);
  logHealth(now);

  // Cooperative yield only; there is intentionally no delay() in runtime.
  taskYIELD();
}
