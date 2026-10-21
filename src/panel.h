#pragma once

#include <stdint.h>

// One HUB75 module is 64x32 on this board (Tronbyt pixoticker target).
static const uint16_t MODULE_W = 64;
static const uint16_t MODULE_H = 32;

struct PanelCfg {
  uint8_t across;   // modules horizontally, 1..4
  uint8_t down;     // modules vertically, 1..4
  uint8_t serpentine;  // 1 when the chain snakes (2x2 wiring)
  uint8_t invertClock; // board wiring: horizontal clock phase inverted
  uint8_t modH;     // module height: 32 (1/16 scan) or 64 (1/32 scan)
};

inline uint16_t panelWidth(const PanelCfg &c) { return MODULE_W * c.across; }
inline uint16_t panelHeight(const PanelCfg &c) { return (c.modH ? c.modH : MODULE_H) * c.down; }

// Returns false when the DMA driver refuses the configuration.
// Caller must arm a watchdog around this: a wrong pin/scan config can hang.
bool panel_begin(const PanelCfg &cfg);

// Stops DMA output. Safe to call after a successful begin only.
void panel_end();

// Draws an RGB565 buffer of panelWidth x panelHeight. No-op in safe mode.
void panel_show(const uint16_t *fb);

void panel_setBrightness(uint8_t level);  // 0..255
void panel_test_pattern();

// true when a panel was initialised this boot.
bool panel_active();
