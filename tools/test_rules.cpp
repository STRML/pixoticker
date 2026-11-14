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

// The invariant: the panel never asks for a frame-free stream (UDP mode)
// unless it holds a token from its last 200 and has had a valid datagram
// within the silence window. Every step is checked against it.
struct Link {
  bool token, alive, steady;
};

static bool walk(UdpMode &m, const Link *steps, int n) {
  for (int i = 0; i < n; i++) {
    udpModeStep(m, steps[i].token, steps[i].alive, steps[i].steady);
    if (m.udp && !(steps[i].token && steps[i].alive)) return false;
  }
  return true;
}

static UdpMode onUdp() {
  UdpMode m;
  udpModeStep(m, true, true, true);
  return m;
}

static void startsOnTcp() {
  UdpMode m;
  check("a fresh boot asks for frames on the stream", !m.udp);
}

static void noTokenEver() {
  UdpMode m;
  Link s[] = {{false, false, false}, {false, true, true}, {false, true, true}};
  check("no token ever: stays on the stream", walk(m, s, 3) && !m.udp);
}

static void relayWithoutUdp() {
  UdpMode m;
  Link s[] = {{true, false, false}, {true, false, false}, {true, false, false}};
  check("a token but never a datagram: stays on the stream", walk(m, s, 3) && !m.udp);
}

static void upgradeOnlyWhenSteady() {
  UdpMode m;
  Link s[] = {{true, true, false}, {true, true, false}};
  check("live but not yet steady: stays on the stream", walk(m, s, 2) && !m.udp);
  check("token and steady UDP: moves to UDP", udpModeStep(m, true, true, true) == UDP_TO_UDP && m.udp);
}

static void tokenStripped() {
  UdpMode m = onUdp();
  check("a 200 without a token drops back to the stream at once",
        udpModeStep(m, false, true, true) == UDP_TO_STREAM && !m.udp);
}

static void blockedAfterUpgrade() {
  UdpMode m = onUdp();
  check("UDP silent after the upgrade drops back to the stream",
        udpModeStep(m, true, false, false) == UDP_TO_STREAM && !m.udp);
}

static void relayRestartOnUdp() {
  // The relay restarts: the old token is dead, hellos go unanswered, UDP goes
  // silent. The stream reconnects with frames and a new token, and UDP comes
  // back once it is steady again.
  UdpMode m = onUdp();
  Link s[] = {{true, true, true}, {true, false, false}, {true, true, false}, {true, true, false}};
  bool ok = walk(m, s, 4) && !m.udp;
  check("a relay restart in UDP mode drops to the stream until UDP is steady again",
        ok && udpModeStep(m, true, true, true) == UDP_TO_UDP);
}

static void reconnectWhileLive() {
  UdpMode m = onUdp();
  Link s[] = {{true, true, true}, {true, true, false}, {true, true, true}};
  check("a stream reconnect while UDP is live does not flap to the stream", walk(m, s, 3) && m.udp);
}

static void randomWalks() {
  uint32_t x = 12345;
  bool ok = true;
  for (int run = 0; run < 2000 && ok; run++) {
    UdpMode m;
    for (int i = 0; i < 50 && ok; i++) {
      x = x * 1664525u + 1013904223u;
      Link l = {(x >> 8 & 1) != 0, (x >> 9 & 1) != 0, false};
      l.steady = l.alive && (x >> 10 & 1);
      ok = walk(m, &l, 1);
    }
  }
  check("2000 random walks never break the invariant", ok);
}

static void otaCurrent() {
  const char *a = "aaaa", *b = "BBBB";
  check("the confirmed image is current", otaIsCurrent("AAAA", a, ""));
  check("the image waiting for its first frame is current too", otaIsCurrent("bbbb", "", b));
  check("another image is not current", !otaIsCurrent("cccc", a, b));
  check("empty records match nothing", !otaIsCurrent("cccc", "", ""));
}

static void otaRollback() {
  // B was installed over A and reset before its first whole frame; the
  // bootloader went back to A, which is confirmed. B's pending hash is stale.
  check("a pending hash beside a confirmed running image is stale", otaPendingStale(true, true));
  check("a pending hash beside an unconfirmed running image is that image", !otaPendingStale(false, true));
  check("nothing pending is never stale", !otaPendingStale(true, false));
  check("after a rollback an op for the failed image is not current", !otaIsCurrent("bbbb", "aaaa", ""));
}

int main() {
  startsOnTcp();
  noTokenEver();
  relayWithoutUdp();
  upgradeOnlyWhenSteady();
  tokenStripped();
  blockedAfterUpgrade();
  relayRestartOnUdp();
  reconnectWhileLive();
  randomWalks();
  otaCurrent();
  otaRollback();
  printf(failed ? "SOME FAILED\n" : "ALL PASS\n");
  return failed ? 1 : 0;
}
