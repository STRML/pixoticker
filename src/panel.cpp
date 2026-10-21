#include "panel.h"

#include <ESP32-HUB75-MatrixPanel-I2S-DMA.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// Pin map for the PixoTicker board (Tronbyt BOARD_PIXOTICKER).
// One 64x32 HUB75 module per chain position.
static const int8_t P_R1 = 2;
static const int8_t P_G1 = 4;
static const int8_t P_B1 = 15;
static const int8_t P_R2 = 16;
static const int8_t P_G2 = 17;
static const int8_t P_B2 = 27;
static const int8_t P_A = 5;
static const int8_t P_B = 18;
static const int8_t P_C = 19;
static const int8_t P_D = 21;
static const int8_t P_E = 12;
static const int8_t P_CLK = 22;
static const int8_t P_LAT = 26;
static const int8_t P_OE = 25;

static MatrixPanel_I2S_DMA *s_matrix = nullptr;
static PanelCfg s_cfg = {2, 2, 0, 1};
static SemaphoreHandle_t s_mu = xSemaphoreCreateRecursiveMutex();

void panel_lock() { xSemaphoreTakeRecursive(s_mu, portMAX_DELAY); }
void panel_unlock() { xSemaphoreGiveRecursive(s_mu); }

struct PanelGuard {
  PanelGuard() { panel_lock(); }
  ~PanelGuard() { panel_unlock(); }
};

bool panel_begin(const PanelCfg &cfg) {
  PanelGuard g;
  if (cfg.across == 0 || cfg.down == 0 || cfg.across * cfg.down > 4) return false;
  s_cfg = cfg;

  HUB75_I2S_CFG::i2s_pins pins = {P_R1, P_G1, P_B1, P_R2, P_G2, P_B2,
                                  P_A,  P_B,  P_C,  P_D,  P_E,
                                  P_LAT, P_OE, P_CLK};

  uint8_t chain = cfg.across * cfg.down;
  uint16_t modH = cfg.modH ? cfg.modH : MODULE_H;
  HUB75_I2S_CFG mx(MODULE_W, modH, chain, pins,
                   HUB75_I2S_CFG::FM6126A,
                   HUB75_I2S_CFG::TYPE138,
                   true,                    // double buffer: draw then flip, no tearing
                   HUB75_I2S_CFG::HZ_10M,   // clock
                   1,                       // latch blanking
                   cfg.invertClock != 0);

  s_matrix = new MatrixPanel_I2S_DMA(mx);
  if (!s_matrix) return false;
  if (!s_matrix->begin()) {
    delete s_matrix;
    s_matrix = nullptr;
    return false;
  }
  s_matrix->setBrightness8(128);
  s_matrix->clearScreen();
  return true;
}

void panel_end() {
  PanelGuard g;
  if (s_matrix) {
    s_matrix->stopDMAoutput();
    delete s_matrix;
    s_matrix = nullptr;
  }
}

bool panel_active() {
  PanelGuard g;
  return s_matrix != nullptr;
}

void panel_setBrightness(uint8_t level) {
  PanelGuard g;
  if (s_matrix) s_matrix->setBrightness8(level);
}

// Map virtual (x,y) to the physical chain position. Modules run left to right,
// rows top to bottom; a serpentine chain reverses every other row.
static inline void map_px(int x, int y, int *ox, int *oy) {
  int mh = s_cfg.modH ? s_cfg.modH : MODULE_H;
  int row = y / mh;
  int col = x / MODULE_W;
  int lx = x % MODULE_W;
  int ly = y % mh;
  int p = row * s_cfg.across + col;
  if (s_cfg.serpentine && (row & 1)) {
    col = s_cfg.across - 1 - col;
    lx = MODULE_W - 1 - lx;
    p = row * s_cfg.across + col;
  }
  *ox = p * MODULE_W + lx;
  *oy = ly;
}

void panel_show(const uint16_t *fb) {
  PanelGuard g;
  if (!s_matrix || !fb) return;
  uint16_t w = panelWidth(s_cfg);
  int mh = s_cfg.modH ? s_cfg.modH : MODULE_H;
  // Walk module by module: no division per pixel, so a full frame lands well
  // inside one DMA refresh and the flip shows a coherent image.
  for (int row = 0; row < s_cfg.down; row++) {
    for (int col = 0; col < s_cfg.across; col++) {
      int p = row * s_cfg.across + col;
      int lx0 = col * MODULE_W;
      if (s_cfg.serpentine && (row & 1)) {
        p = row * s_cfg.across + (s_cfg.across - 1 - col);
        lx0 = (s_cfg.across - 1 - col) * MODULE_W;
      }
      int base = row * mh;
      for (int y = 0; y < mh; y++) {
        const uint16_t *src = fb + (size_t)(base + y) * w + lx0;
        int py = (row * mh) + y;
        for (int x = 0; x < MODULE_W; x++) {
          int px = p * MODULE_W + ((s_cfg.serpentine && (row & 1)) ? (MODULE_W - 1 - x) : x);
          s_matrix->drawPixel(px, py, src[x]);
        }
      }
    }
  }
  s_matrix->flipDMABuffer();
}

// Ruler: white border, corner markers, 16 px ticks. Enough to identify
// orientation, module boundaries and missing panels at a glance.
void panel_test_pattern() {
  uint16_t w = panelWidth(s_cfg);
  uint16_t h = panelHeight(s_cfg);
  uint16_t *fb = (uint16_t *)calloc((size_t)w * h, sizeof(uint16_t));
  if (!fb) return;
  const uint16_t WHITE = 0xFFFF, RED = 0xF800, GREEN = 0x07E0, BLUE = 0x001F, DIM = 0x4208;
  for (int x = 0; x < w; x++) {
    fb[x] = WHITE;
    fb[(size_t)(h - 1) * w + x] = WHITE;
    if (x % 16 == 0)
      for (int y = 0; y < 4; y++) fb[(size_t)y * w + x] = DIM;
  }
  for (int y = 0; y < h; y++) {
    fb[(size_t)y * w] = WHITE;
    fb[(size_t)y * w + (w - 1)] = WHITE;
    if (y % 16 == 0)
      for (int x = 0; x < 4; x++) fb[(size_t)y * w + x] = DIM;
  }
  for (int y = 0; y < 8; y++)
    for (int x = 0; x < 8; x++) {
      fb[(size_t)y * w + x] = RED;                              // top-left
      fb[(size_t)y * w + (w - 1 - x)] = GREEN;                  // top-right
      fb[(size_t)(h - 1 - y) * w + x] = BLUE;                   // bottom-left
      fb[(size_t)(h - 1 - y) * w + (w - 1 - x)] = WHITE;        // bottom-right
    }
  panel_show(fb);
  free(fb);
}
