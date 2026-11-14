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

// Whether an ota op's image is already on the device: its SHA-256 (of the
// .bin) against the images in the running and the boot partitions, hashed
// from flash. No record is kept, so there is nothing to get out of step.
static void otaImages() {
  const char *A = "aaaa", *B = "bbbb", *C = "cccc";
  check("the running image matches (case does not matter)", otaImageHere("AAAA", A, A));
  check("an image that is neither running nor booting next is not here", !otaImageHere(C, A, A));
  check("no hashes yet match nothing", !otaImageHere(C, "", ""));
  // /u wrote B and committed it; the restart has not happened yet. Running is
  // A, boot is B. A re-posted op for B must not install it again.
  check("after a /u install, before its restart, an op for the new image is skipped", otaImageHere(B, A, B));
  check("after a /u install, an op for a third image is not", !otaImageHere(C, A, B));
  // B ran on trial, reset before its first whole frame, and the bootloader
  // went back to A: running and boot are both A.
  check("after a rollback an op for the failed image installs it again", !otaImageHere(B, A, A));
  check("after a rollback an op for the image now running is skipped", otaImageHere(A, A, A));
  // B confirmed its first frame, then power was lost before anything else:
  // the flash still holds B in both.
  check("after power loss mid-confirm the running image is still known", otaImageHere(B, B, B));
}

static void resetNames() {
  // esp_reset_reason_t values; src/main.cpp static_asserts the ones named here.
  check("reset 1 is poweron", !strcmp(resetName(1), "poweron"));
  check("reset 3 is sw", !strcmp(resetName(3), "sw"));
  check("reset 4 is panic", !strcmp(resetName(4), "panic"));
  check("reset 6 is task_wdt", !strcmp(resetName(6), "task_wdt"));
  check("reset 9 is brownout", !strcmp(resetName(9), "brownout"));
  check("an unknown reset code names itself unknown",
        !strcmp(resetName(-1), "unknown") && !strcmp(resetName(99), "unknown"));
}

static void bootCounting() {
  // Codes are esp_reset_reason_t's (see resetName).
  check("a panic counts as a failed boot", bootCounts(4, false));
  check("a task watchdog counts as a failed boot", bootCounts(6, false));
  check("an interrupt watchdog counts as a failed boot", bootCounts(5, false));
  check("a brownout counts as a failed boot", bootCounts(9, false));
  check("the panel guard's restart counts as a failed boot", bootCounts(3, true));
  check("power on does not count", !bootCounts(1, false));
  check("the RTC watchdog (wdt: a hang in the bootloader, as after a slow power-up) does not count",
        !bootCounts(7, false));
  check("the EN pin (a serial port opening) does not count", !bootCounts(2, false));
  check("a restart the firmware asked for does not count", !bootCounts(3, false));
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
  otaImages();
  resetNames();
  bootCounting();
  printf(failed ? "SOME FAILED\n" : "ALL PASS\n");
  return failed ? 1 : 0;
}
