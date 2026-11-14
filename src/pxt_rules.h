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

// At boot, what to do with the pending hash, which names the image waiting for
// its first whole frame and the slot it was written to.
//   Another slot is running: that image reset before proving itself and the
//   bootloader went back. Drop the hash, or it would count as current and be
//   promoted when this image draws.
//   Its own slot, already confirmed: power was lost between the confirm and
//   the promotion. Promote it now.
//   Its own slot, unconfirmed: this is the image on trial. Keep it.
// A hash with no slot comes from older firmware; only an unconfirmed image
// can be its owner.
enum PendingFate { PENDING_KEEP, PENDING_DROP, PENDING_PROMOTE };

inline PendingFate otaPendingFate(bool has_pending, const char *pending_slot, const char *running_slot,
                                  bool running_confirmed) {
  if (!has_pending) return PENDING_KEEP;
  if (!pending_slot[0]) return running_confirmed ? PENDING_DROP : PENDING_KEEP;
  if (strcasecmp(pending_slot, running_slot)) return PENDING_DROP;
  return running_confirmed ? PENDING_PROMOTE : PENDING_KEEP;
}

// Why the previous boot ended, as /state and X-Pxt-State report it. The codes
// are esp_reset_reason_t's; src/main.cpp static_asserts them.
inline const char *resetName(int r) {
  static const char *const names[] = {"unknown", "poweron", "ext",      "sw",   "panic",     "int_wdt",
                                      "task_wdt", "wdt",    "deepsleep", "brownout", "sdio", "usb",
                                      "jtag",     "efuse",  "pwr_glitch", "cpu_lockup"};
  return (r >= 0 && r < (int)(sizeof(names) / sizeof(names[0]))) ? names[r] : "unknown";
}

// A first whole frame proves the image that drew it. The pending hash may be
// promoted only from that image's own slot: an upload records B as pending
// while A is still running, and A's frame must not promote B. A hash with no
// slot comes from older firmware and promotes as before.
inline bool otaMayPromote(const char *pending_slot, const char *running_slot) {
  return !pending_slot[0] || !strcasecmp(pending_slot, running_slot);
}

// Whether the boot that just ended counts toward safe mode: a crash, a
// watchdog, a brownout, or the panel guard's restart. Power on, the EN pin
// (opening the serial port pulses it) and restarts the firmware asked for do
// not. Codes are esp_reset_reason_t's.
inline bool bootCounts(int reason, bool guard_fired) {
  switch (reason) {
    case 4:  // panic
    case 5:  // int_wdt
    case 6:  // task_wdt
    case 7:  // wdt
    case 9:  // brownout
    case 15:  // cpu_lockup
      return true;
    default:
      return guard_fired;
  }
}
