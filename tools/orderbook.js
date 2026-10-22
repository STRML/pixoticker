#!/usr/bin/env bun
// Live Deribit BTC-PERPETUAL orderbook on the panel.
//
//   PXT_RELAY_KEY=... bun tools/orderbook.js [host] [fps] [seconds]
//
// Subscribes to book.BTC-PERPETUAL.100ms over WebSocket, keeps the book in
// sync, renders cumulative bid/ask depth as two-sided bars and pushes RGB565
// frames to POST /frame. The ESP32 never touches the internet.
//
// The 100ms channel sends `[action, price, amount]` triples in both snapshots
// and changes (action is "new", "change" or "delete"), not the `[price, amount]`
// pairs of the old none.N channels.

const HOST = process.argv[2] ?? "192.168.4.1";
// Direct pushes at 15 fps wedged the panel's web server; 8 has held.
const FPS = Number(process.argv[3] ?? 8);
const SECS = Number(process.argv[4] ?? 0); // 0 = run until killed
const KEY = process.env.PXT_RELAY_KEY ?? "";
const WS_URL = "wss://www.deribit.com/ws/api/v2";

const state = await (await fetch(`http://${HOST}/state`)).json();
const W = state.width;
const H = state.height;
const CX = W >> 1;
console.log(`panel ${W}x${H} at ${HOST}, ${FPS} fps`);

let bids = new Map(); // price -> amount
let asks = new Map();
let lastPrice = 0;
let dirty = true;
let snapshots = 0;
let changes = 0;

function applyChanges(side, changesBag) {
  const m = side === "bids" ? bids : asks;
  for (const [action, price, amount] of changesBag) {
    if (action === "delete") m.delete(price);
    else m.set(price, amount);
  }
}

// Snapshot entries use the same [action, price, amount] shape as changes.
function reset(side, levels) {
  const m = side === "bids" ? bids : asks;
  m.clear();
  for (const [action, price, amount] of levels) {
    if (action !== "delete") m.set(price, amount);
  }
}

const ws = new WebSocket(WS_URL);
ws.onopen = () => {
  ws.send(JSON.stringify({
    jsonrpc: "2.0", id: 1, method: "public/subscribe",
    params: { channels: ["book.BTC-PERPETUAL.100ms"] },
  }));
};
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method !== "subscription") return;
  const d = msg.params?.data;
  if (!d) return;
  if (d.type === "snapshot") {
    reset("bids", d.bids);
    reset("asks", d.asks);
    if (d.last_price) lastPrice = d.last_price;
    snapshots++;
    console.log(`snapshot: ${d.bids.length} bids ${d.asks.length} asks`);
  } else if (d.type === "change") {
    applyChanges("bids", d.bids ?? []);
    applyChanges("asks", d.asks ?? []);
    changes++;
  }
  dirty = true;
};
ws.onclose = () => {
  console.error("ws closed");
  process.exit(1);
};

// The panel's wiring swaps G and B, so raw RGB565 frames carry them swapped.
function rgb565(r, g, b) {
  return (((r & 0xf8) << 8) | ((b & 0xfc) << 3) | (g >> 3)) & 0xffff;
}

function render() {
  const fb = new Uint16Array(W * H);
  const bidsSorted = [...bids.entries()].sort((a, b) => b[0] - a[0]).slice(0, 20);
  const asksSorted = [...asks.entries()].sort((a, b) => a[0] - b[0]).slice(0, 20);

  let cumB = 0, cumA = 0;
  let maxCum = 1;
  const bc = bidsSorted.map(([, a]) => (cumB += a));
  const ac = asksSorted.map(([, a]) => (cumA += a));
  maxCum = Math.max(cumB, cumA, 1);

  const half = CX - 1;
  const rowH = Math.max(1, Math.floor((H - 8) / 20));
  const drawBars = (cums, color, dir) => {
    cums.forEach((c, i) => {
      const len = Math.round((c / maxCum) * half);
      const y0 = 2 + i * rowH;
      for (let y = y0; y < Math.min(y0 + rowH - 1, H - 6); y++) {
        for (let k = 0; k < len; k++) {
          const x = dir < 0 ? CX - 1 - k : CX + k;
          if (x >= 0 && x < W) fb[y * W + x] = color;
        }
      }
    });
  };
  drawBars(bc, rgb565(0, 160, 80), -1);
  drawBars(ac, rgb565(200, 40, 40), +1);

  // center line and last price marker
  for (let y = 0; y < H - 4; y++) fb[y * W + CX] = rgb565(60, 60, 60);
  const best = bidsSorted[0]?.[0] ?? 0;
  const spread = (asksSorted[0]?.[0] ?? 0) - best;
  if (best) {
    const t = Math.min(1, Math.max(0, spread / Math.max(best * 0.002, 1e-9)));
    for (let x = 0; x < W; x++) {
      const y = H - 3 - Math.round(t * 4);
      if (y >= 0) fb[y * W + x] = rgb565(255, 255, 255);
    }
  }
  // footer pulse: ticks per update, so a frozen stream is visible
  for (let x = 0; x < W; x += 4) fb[(H - 1) * W + x] = rgb565(0, 90, 160);

  return fb;
}

async function push(fb) {
  const raw = new Uint8Array(W * H * 2);
  for (let i = 0; i < W * H; i++) {
    raw[i * 2] = fb[i] >> 8;
    raw[i * 2 + 1] = fb[i] & 0xff;
  }
  // base64: the ESP32 web server keeps a plain body in a NUL-terminated string
  const body = Buffer.from(raw).toString("base64");
  try {
    const r = await fetch(`http://${HOST}/frame?fmt=rgb565`, {
      method: "POST", body, headers: { "Content-Type": "text/plain", "X-Api-Key": KEY },
    });
    if (!r.ok) console.error("push", r.status);
  } catch (e) {
    console.error("push failed", e.message);
  }
}

if (SECS > 0) setTimeout(() => process.exit(0), SECS * 1000);

let frames = 0;
setInterval(async () => {
  if (!dirty) return;
  dirty = false;
  await push(render());
  if (++frames % FPS === 0) {
    const bestBid = [...bids.keys()].reduce((a, b) => Math.max(a, b), 0);
    process.stdout.write(
      `\rframes ${frames} snap ${snapshots} chg ${changes} best ${bestBid} levels ${bids.size}/${asks.size}  `);
  }
}, Math.max(10, Math.floor(1000 / FPS)));
