// Virtual display: mirror a same-origin canvas to the PixoTicker panel.
//
// Paste into the page console (or inject with tab.evaluate/evaluate). The page
// does the capture and the upload, so the host Mac only runs the browser.
//
//   window.__PXT_HOST = '192.168.4.1';   // default
//   window.__PXT_KEY = '...';            // the panel key, if the build has one
//   window.__mirror.running = false;      // stop
//
// WebGL canvases clear their drawing buffer after compositing, so a capture
// from a timer reads an empty buffer half the time. Hooking requestAnimation-
// Frame and reading right after the page's own draw callback avoids that.
// Assumes the panel firmware: 128x64, G/B swapped wiring.
(() => {
  const HOST = window.__PXT_HOST || "192.168.4.1";
  const W = 128, H = 64;
  const src = document.querySelector("canvas");
  if (!src) return "no canvas";
  if (window.__mirror) return "already running";

  // letterbox the 4:3 source into the 2:1 panel
  const TH = H, TW = Math.round((src.width / src.height) * TH), OX = Math.floor((W - TW) / 2);
  const off = document.createElement("canvas");
  off.width = W;
  off.height = H;
  const ctx = off.getContext("2d", { willReadFrequently: true });

  window.__mirror = { sent: 0, fails: 0, skipped: 0, host: HOST, running: true };
  let prev = null;
  let last = 0;

  const capture = () => {
    if (!window.__mirror.running) return;
    const now = performance.now();
    if (now - last < 90) return; // ~11 fps ceiling
    last = now;
    try {
      ctx.fillStyle = "#000";
      ctx.fillRect(0, 0, W, H);
      ctx.drawImage(src, OX, 0, TW, TH);
      const d = ctx.getImageData(0, 0, W, H).data;
      const raw = new Uint8Array(W * H * 2);
      for (let i = 0, j = 0; i < W * H; i++, j += 4) {
        const r = d[j], g = d[j + 1], b = d[j + 2];
        // RGB565 big-endian; panel wiring swaps G and B
        const v = ((r & 0xf8) << 8) | ((b & 0xfc) << 3) | (g >> 3);
        raw[i * 2] = v >> 8;
        raw[i * 2 + 1] = v & 0xff;
      }
      let same = prev && prev.length === raw.length;
      if (same) for (let i = 0; i < raw.length; i++) if (prev[i] !== raw[i]) { same = false; break; }
      prev = raw;
      if (same) { window.__mirror.skipped++; return; }

      let bin = "";
      for (let i = 0; i < raw.length; i += 4096)
        bin += String.fromCharCode.apply(null, raw.subarray(i, i + 4096));
      fetch(`http://${HOST}/frame?fmt=rgb565`, {
        method: "POST",
        mode: "cors", // the panel answers the preflight for X-Api-Key
        headers: { "Content-Type": "text/plain", "X-Api-Key": window.__PXT_KEY || "" },
        body: btoa(bin),
      });
      window.__mirror.sent++;
    } catch (e) {
      window.__mirror.fails++;
    }
  };

  if (!window.__mirrorRAF) {
    const orig = window.requestAnimationFrame.bind(window);
    window.__mirrorRAF = true;
    window.requestAnimationFrame = (cb) => orig((t) => { cb(t); capture(); });
  }
  return "mirror started";
})();
