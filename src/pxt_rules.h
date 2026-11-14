#pragma once
// Pure decisions the firmware makes, kept free of Arduino types so
// tools/test_rules.cpp can check them on the host.

#include <stdint.h>
#include <strings.h>

// Which channel carries frames. UDP until it has been silent for `silent_ms`
// since the panel first held a stream token in this mode; then the stream,
// until UDP has been steady again. Called from every loop of the pull task,
// whether or not a stream is open.
struct UdpMode {
  bool udp = true;
  bool timing = false;    // this mode has held a token
  uint32_t token_at = 0;  // millis() when it first did
};

enum UdpSwitch { UDP_STAY, UDP_TO_STREAM, UDP_TO_UDP };

inline UdpSwitch udpModeStep(UdpMode &m, uint32_t now, bool token, bool alive, bool steady,
                             uint32_t silent_ms) {
  if (!m.udp) {
    if (!steady) return UDP_STAY;
    m.udp = true;
    m.timing = token;
    m.token_at = now;
    return UDP_TO_UDP;
  }
  // A relay that hands out no token (an old one) sends frames on the stream
  // anyway, so there is nothing to fall back from.
  if (!token) return UDP_STAY;
  if (!m.timing) {
    m.timing = true;
    m.token_at = now;
  }
  if (alive || now - m.token_at <= silent_ms) return UDP_STAY;
  m.udp = false;
  m.timing = false;
  return UDP_TO_STREAM;
}

// An OTA op for the image already confirmed, or for the one installed and
// still waiting for its first whole frame, must not reinstall it: that would
// restart the panel and could overwrite the last good slot before the new
// image proves itself.
inline bool otaIsCurrent(const char *sha, const char *installed, const char *pending) {
  return (installed[0] && !strcasecmp(sha, installed)) || (pending[0] && !strcasecmp(sha, pending));
}
