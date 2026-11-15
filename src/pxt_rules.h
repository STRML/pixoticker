#pragma once
// Pure decisions the firmware makes, kept free of Arduino types so
// tools/test_rules.cpp can check them on the host.

#include <stdint.h>
#include <strings.h>

// Which channel carries frames. The panel starts with frames on the TLS
// stream and moves them to UDP only once it holds a token from its last 200
// and UDP has been steady. It drops back to the stream the moment either
// fails. The invariant: it never asks for a frame-free stream without a
// token and a valid datagram within the silence window. Called on every pass
// of the pull task, with or without an open stream.
struct UdpMode {
  bool udp = false;
};

enum UdpSwitch { UDP_STAY, UDP_TO_STREAM, UDP_TO_UDP };

inline UdpSwitch udpModeStep(UdpMode &m, bool token, bool alive, bool steady) {
  if (m.udp && !(token && alive)) {
    m.udp = false;
    return UDP_TO_STREAM;
  }
  if (!m.udp && token && alive && steady) {
    m.udp = true;
    return UDP_TO_UDP;
  }
  return UDP_STAY;
}

// Whether an ota op's image is already on the device: its SHA-256, which is
// of the .bin, against the image in the running partition and the one in the
// boot partition, both hashed from flash. Running covers an image on trial
// and one whose confirm power cut short; boot covers an install waiting for
// its restart. Nothing is recorded, so nothing can disagree with the flash.
inline bool otaImageHere(const char *sha, const char *running_hash, const char *boot_hash) {
  return (running_hash[0] && !strcasecmp(sha, running_hash)) || (boot_hash[0] && !strcasecmp(sha, boot_hash));
}

// What to do with an ota op. A hash that is "" is unknown (a flash read
// failed, or an install moved the boot image mid-hash), never a mismatch:
// installing then could put the running image into the other slot, over the
// last good one. So install only when both hashes are known and neither
// matches; otherwise wait for the next op.
enum OtaDecision { OTA_SKIP, OTA_INSTALL, OTA_WAIT };

inline OtaDecision otaDecide(const char *sha, const char *running_hash, const char *boot_hash) {
  if (otaImageHere(sha, running_hash, boot_hash)) return OTA_SKIP;
  return (running_hash[0] && boot_hash[0]) ? OTA_INSTALL : OTA_WAIT;
}

// Why the previous boot ended, as /state and X-Pxt-State report it. The codes
// are esp_reset_reason_t's; src/main.cpp static_asserts them.
inline const char *resetName(int r) {
  static const char *const names[] = {"unknown", "poweron", "ext",      "sw",   "panic",     "int_wdt",
                                      "task_wdt", "wdt",    "deepsleep", "brownout", "sdio", "usb",
                                      "jtag",     "efuse",  "pwr_glitch", "cpu_lockup"};
  return (r >= 0 && r < (int)(sizeof(names) / sizeof(names[0]))) ? names[r] : "unknown";
}

// Whether the boot that just ended counts toward safe mode: a crash, the
// app's interrupt or task watchdog, a brownout, or the panel guard's restart.
// Power on, the EN pin (opening the serial port pulses it) and restarts the
// firmware asked for do not. Nor does wdt: on the ESP32 that is the RTC
// watchdog, armed only while the bootloader runs, so it means the boot hung
// before the app (a slow power-up), which safe mode cannot help. Codes are
// esp_reset_reason_t's.
inline bool bootCounts(int reason, bool guard_fired) {
  switch (reason) {
    case 4:  // panic
    case 5:  // int_wdt
    case 6:  // task_wdt
    case 9:  // brownout
    case 15:  // cpu_lockup
      return true;
    default:
      return guard_fired;
  }
}
