// Host checks for the panel's pure decisions in src/pxt_rules.h: which channel
// carries frames, and whether an OTA op names an image already on the device.
//
//   c++ -std=c++17 -Isrc tools/test_rules.cpp -o /tmp/test_rules && /tmp/test_rules
//
// Prints one PASS/FAIL line per check and exits 1 if any fails.
#include <cstdio>
#include <cstring>

#include "pxt_rules.h"

static int failed = 0;

static void check(const char *name, bool ok) {
  printf("%s  %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failed++;
}

static const uint32_t SILENT = 10000;

static void slowFirstToken() {
  // The panel boots, but the relay answers its first stream a minute later.
  // The silence clock must start when a token arrives, not at task start.
  UdpMode m;
  check("no token yet: frames stay on UDP", udpModeStep(m, 1000, false, false, false, SILENT) == UDP_STAY);
  check("token arrives late: no instant fallback", udpModeStep(m, 60000, true, false, false, SILENT) == UDP_STAY);
  check("still within 10 s of the token: stay", udpModeStep(m, 69000, true, false, false, SILENT) == UDP_STAY);
  check("10 s of token with no datagram: frames move to the stream",
        udpModeStep(m, 70001, true, false, false, SILENT) == UDP_TO_STREAM && !m.udp);
}

static void reconnectsDoNotReset() {
  // A stream that reconnects every few seconds keeps the same token clock,
  // so a dead UDP path still falls back.
  UdpMode m;
  udpModeStep(m, 1000, true, false, false, SILENT);
  for (uint32_t t = 3000; t <= 10000; t += 3000) udpModeStep(m, t, true, false, false, SILENT);
  check("reconnects do not restart the silence clock",
        udpModeStep(m, 11001, true, false, false, SILENT) == UDP_TO_STREAM);
}

static void aliveStays() {
  UdpMode m;
  udpModeStep(m, 1000, true, false, false, SILENT);
  check("live datagrams keep frames on UDP", udpModeStep(m, 50000, true, true, false, SILENT) == UDP_STAY && m.udp);
}

static void backToUdpWithoutStream() {
  // On the stream, with TLS down, UDP is steady again: the outer loop's step
  // must move frames back without waiting for a stream that will not open.
  UdpMode m;
  m.udp = false;
  check("steady UDP moves frames back", udpModeStep(m, 5000, true, true, true, SILENT) == UDP_TO_UDP && m.udp);
  check("after the move back the silence clock starts over",
        udpModeStep(m, 5000 + SILENT, true, false, false, SILENT) == UDP_STAY);
}

static void noTokenNoFallback() {
  // An old relay hands out no token and sends frames on the stream anyway.
  UdpMode m;
  check("no token ever: never falls back", udpModeStep(m, 600000, false, false, false, SILENT) == UDP_STAY);
}

static void otaCurrent() {
  const char *a = "aaaa", *b = "BBBB";
  check("the confirmed image is current", otaIsCurrent("AAAA", a, ""));
  check("the image waiting for its first frame is current too", otaIsCurrent("bbbb", "", b));
  check("another image is not current", !otaIsCurrent("cccc", a, b));
  check("empty records match nothing", !otaIsCurrent("cccc", "", ""));
}

int main() {
  slowFirstToken();
  reconnectsDoNotReset();
  aliveStays();
  backToUdpWithoutStream();
  noTokenNoFallback();
  otaCurrent();
  printf(failed ? "SOME FAILED\n" : "ALL PASS\n");
  return failed ? 1 : 0;
}
