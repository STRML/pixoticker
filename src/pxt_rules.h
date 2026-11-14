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

// An OTA op for the image already confirmed, or for the one installed and
// still waiting for its first whole frame, must not reinstall it: that would
// restart the panel and could overwrite the last good slot before the new
// image proves itself.
inline bool otaIsCurrent(const char *sha, const char *installed, const char *pending) {
  return (installed[0] && !strcasecmp(sha, installed)) || (pending[0] && !strcasecmp(sha, pending));
}

// Each slot keeps its own pending hash: the image installed there that has not
// shown a whole frame yet. An install writes only its target slot's record,
// before it commits, so a failed install never touches the image on trial.
// At boot, what to do with each record:
//   Another slot's: that image is not running (a rollback, or an install that
//   never committed). Drop it.
//   The running slot's, already confirmed: power was lost between the confirm
//   and the promotion. Promote it now.
//   The running slot's, unconfirmed: this is the image on trial. Keep it.
enum PendingFate { PENDING_KEEP, PENDING_DROP, PENDING_PROMOTE };

inline PendingFate otaSlotFate(bool is_running_slot, bool running_confirmed) {
  if (!is_running_slot) return PENDING_DROP;
  return running_confirmed ? PENDING_PROMOTE : PENDING_KEEP;
}

// Where the old single pending record (otapending, otaslot) belongs. Its slot
// when it names one. Without a slot, older firmware wrote it for the image it
// had just installed, which is the one now running if that image is still on
// trial; once confirmed, the record belongs to nothing.
inline const char *legacyPendingSlot(const char *legacy_slot, const char *running_slot, bool running_confirmed) {
  if (legacy_slot[0]) return legacy_slot;
  return running_confirmed ? "" : running_slot;
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
