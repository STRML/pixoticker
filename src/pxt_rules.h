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

// The pending hash names the image waiting for its first whole frame. If the
// running image is already confirmed, that image never proved itself and the
// bootloader went back: the hash is stale and must not count as current, or
// be promoted when this image draws.
inline bool otaPendingStale(bool running_confirmed, bool has_pending) {
  return running_confirmed && has_pending;
}
