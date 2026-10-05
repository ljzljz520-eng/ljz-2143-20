#include "ppm.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

bool ppm_load_texture(SDL_Renderer *renderer, const char *path,
                      SDL_Texture **out) {
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        fprintf(stderr, "ppm: cannot open %s\n", path);
        return false;
    }
    char magic[3] = {0};
    int width = 0;
    int height = 0;
    int maxval = 0;
    if (fscanf(f, "%2s %d %d %d", magic, &width, &height, &maxval) != 4 ||
        strcmp(magic, "P6") != 0 || width <= 0 || height <= 0 ||
        (maxval != 255)) {
        fprintf(stderr, "ppm: unsupported header in %s\n", path);
        fclose(f);
        return false;
    }
    fgetc(f); /* single whitespace after maxval */

    size_t n = (size_t)width * (size_t)height * 3U;
    unsigned char *rgb = malloc(n);
    if (rgb == NULL) {
        fclose(f);
        return false;
    }
    if (fread(rgb, 1, n, f) != n) {
        fprintf(stderr, "ppm: short pixel data in %s\n", path);
        free(rgb);
        fclose(f);
        return false;
    }
    fclose(f);

    SDL_Surface *surface = SDL_CreateRGBSurfaceWithFormatFrom(
        rgb, width, height, 24, width * 3, SDL_PIXELFORMAT_RGB24);
    if (surface == NULL) {
        fprintf(stderr, "ppm: surface failed: %s\n", SDL_GetError());
        free(rgb);
        return false;
    }
    SDL_Texture *texture = SDL_CreateTextureFromSurface(renderer, surface);
    SDL_FreeSurface(surface);
    free(rgb);
    if (texture == NULL) {
        fprintf(stderr, "ppm: texture failed: %s\n", SDL_GetError());
        return false;
    }
    *out = texture;
    return true;
}

bool ppm_save_framebuffer(SDL_Renderer *renderer, int width, int height,
                          const char *path) {
    size_t n = (size_t)width * (size_t)height * 3U;
    unsigned char *rgb = malloc(n);
    if (rgb == NULL) {
        return false;
    }
    bool ok = false;
    if (SDL_RenderReadPixels(renderer, NULL, SDL_PIXELFORMAT_RGB24, rgb,
                             width * 3) == 0) {
        FILE *f = fopen(path, "wb");
        if (f != NULL) {
            fprintf(f, "P6\n%d %d\n255\n", width, height);
            if (fwrite(rgb, 1, n, f) == n) {
                ok = true;
            }
            fclose(f);
        }
    }
    free(rgb);
    return ok;
}
