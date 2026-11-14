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
