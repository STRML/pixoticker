// Host checks for the panel's pure decisions in src/pxt_rules.h: which channel
// carries frames, and whether an OTA op names an image already on the device.
//
//   c++ -std=c++17 -Isrc tools/test_rules.cpp -o /tmp/test_rules && /tmp/test_rules
//
// Prints one PASS/FAIL line per check and exits 1 if any fails.
#include <cstdio>
#include <cstring>
#include <map>
#include <string>

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

// The device's OTA records, driven by the same rules as src/main.cpp: one
// pending hash per slot, written by an install before it commits; the
// installed hash, written when the running slot's image proves itself.
struct Records {
  std::map<std::string, std::string> pend;  // slot -> hash on trial there
  std::string installed;
  std::string running = "app0";
  bool confirmed = true;

  void install(const std::string &slot, const std::string &sha, bool commits) {
    pend[slot] = sha;  // recorded before the commit
    (void)commits;     // a failed commit leaves the record; it names a slot that never runs
  }
  void boot(const std::string &slot, bool was_confirmed) {
    running = slot;
    confirmed = was_confirmed;
    for (auto it = pend.begin(); it != pend.end();) {
      PendingFate f = otaSlotFate(it->first == running, confirmed);
      if (f == PENDING_PROMOTE) installed = it->second;
      if (f != PENDING_KEEP) it = pend.erase(it);
      else ++it;
    }
  }
  void firstFrame() {  // confirmImage -> promoteInstalled: the running slot only
    confirmed = true;
    auto it = pend.find(running);
    if (it == pend.end()) return;
    installed = it->second;
    pend.erase(it);
  }
  bool current(const std::string &sha) {
    auto it = pend.find(running);
    return otaIsCurrent(sha.c_str(), installed.c_str(), it == pend.end() ? "" : it->second.c_str());
  }
};

static void otaRecords() {
  {
    // B installed into app1, on trial; it draws and is promoted.
    Records r;
    r.installed = "aaaa";
    r.install("app1", "bbbb", true);
    r.boot("app1", false);
    check("B on trial: an op for B is current", r.current("bbbb"));
    r.firstFrame();
    check("B's first frame promotes B", r.installed == "bbbb" && r.pend.empty());
  }
  {
    // A in app0 is running; B is uploaded into app1 and A draws before the restart.
    Records r;
    r.installed = "aaaa";
    r.confirmed = false;
    r.install("app1", "bbbb", true);
    r.firstFrame();
    check("a frame from the old image does not promote the new image", r.installed == "aaaa");
  }
  {
    // Round 2: B in app1 on trial, installed still names A. C is uploaded into
    // app0 and its commit fails. B then draws.
    Records r;
    r.installed = "aaaa";
    r.install("app1", "bbbb", true);
    r.boot("app1", false);
    r.install("app0", "cccc", false);
    check("a failed install does not make its image current", !r.current("cccc"));
    r.firstFrame();
    check("the image on trial still promotes after another install fails", r.installed == "bbbb");
    r.boot("app1", true);
    check("the failed install's record is dropped at the next boot", r.pend.empty());
    check("then an op for A is not current", !r.current("aaaa"));
  }
  {
    // B reset before its first whole frame; the bootloader went back to A.
    Records r;
    r.installed = "aaaa";
    r.install("app1", "bbbb", true);
    r.boot("app1", false);
    r.boot("app0", true);
    check("after a rollback B's record is dropped and an op for B is not current",
          r.pend.empty() && !r.current("bbbb") && r.installed == "aaaa");
  }
  {
    // Power lost between B's confirm and its promotion.
    Records r;
    r.installed = "aaaa";
    r.install("app1", "bbbb", true);
    r.boot("app1", false);
    r.boot("app1", true);
    check("a confirm cut short is promoted at the next boot", r.installed == "bbbb" && r.pend.empty());
  }
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
  otaCurrent();
  otaRecords();
  resetNames();
  bootCounting();
  printf(failed ? "SOME FAILED\n" : "ALL PASS\n");
  return failed ? 1 : 0;
}
