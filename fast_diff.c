#include <stdint.h>
#include <string.h>

/*
 * Compara dos frames RGB24 (w * h * 3 bytes) divididos en tiles de tamaño tile_sz.
 * Rellena dirty_flags[num_tiles] con 1 si el tile cambió, 0 si es idéntico.
 * Devuelve el número de tiles modificados.
 */
int diff_tiles_rgb(const uint8_t *cur, const uint8_t *prev, int w, int h,
                   int tile_sz, uint8_t *dirty_flags) {
    int cols = (w + tile_sz - 1) / tile_sz;
    int rows = (h + tile_sz - 1) / tile_sz;
    int stride = w * 3;
    int dirty_count = 0;

    for (int ty = 0; ty < rows; ty++) {
        int y0 = ty * tile_sz;
        int y1 = y0 + tile_sz;
        if (y1 > h) y1 = h;

        for (int tx = 0; tx < cols; tx++) {
            int x0 = tx * tile_sz;
            int x1 = x0 + tile_sz;
            if (x1 > w) x1 = w;
            int row_bytes = (x1 - x0) * 3;
            int changed = 0;

            for (int y = y0; y < y1; y++) {
                int off = y * stride + x0 * 3;
                if (memcmp(cur + off, prev + off, (size_t)row_bytes) != 0) {
                    changed = 1;
                    break;
                }
            }
            dirty_flags[ty * cols + tx] = (uint8_t)changed;
            dirty_count += changed;
        }
    }
    return dirty_count;
}
