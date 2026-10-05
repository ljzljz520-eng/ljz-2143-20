#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <SDL2/SDL.h>
#include <SDL2/SDL_image.h>

#include "renderer.h"
#include "window.h"

#define WINDOW_TITLE "Visual Window App"
#define WINDOW_WIDTH 1280
#define WINDOW_HEIGHT 720
#define BACKGROUND_IMAGE_PATH "assets/background.png"

static bool init_sdl(void) {
    if (SDL_Init(SDL_INIT_VIDEO) != 0) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return false;
    }

    int img_flags = IMG_INIT_PNG | IMG_INIT_JPG;
    int initted = IMG_Init(img_flags);
    if ((initted & img_flags) == 0) {
        fprintf(stderr, "IMG_Init failed: %s\n", IMG_GetError());
        SDL_Quit();
        return false;
    }

    return true;
}

static void shutdown_sdl(void) {
    IMG_Quit();
    SDL_Quit();
}

static int parse_smoke_frames(void) {
    const char *value = getenv("VDV_SMOKE_FRAMES");
    if (value == NULL || value[0] == '\0') {
        return 0;
    }

    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    if (end == value || *end != '\0' || parsed < 0) {
        return 0;
    }
    if (parsed > 1000000L) {
        parsed = 1000000L;
    }
    return (int)parsed;
}

static void json_escape(FILE *out, const char *value) {
    if (value == NULL) {
        fputs("null", out);
        return;
    }
    fputc('"', out);
    for (const unsigned char *p = (const unsigned char *)value; *p != '\0'; ++p) {
        unsigned char c = *p;
        switch (c) {
            case '"': fputs("\\\"", out); break;
            case '\\': fputs("\\\\", out); break;
            case '\n': fputs("\\n", out); break;
            case '\r': fputs("\\r", out); break;
            case '\t': fputs("\\t", out); break;
            default:
                if (c < 0x20) {
                    fprintf(out, "\\u%04x", c);
                } else {
                    fputc((int)c, out);
                }
        }
    }
    fputc('"', out);
}

static void write_ready_marker(SDL_Window *window, SDL_Renderer *renderer) {
    const char *path = getenv("VDV_FIRST_FRAME_FILE");
    if (path == NULL || path[0] == '\0') {
        return;
    }

    char driver[64] = "unknown";
    SDL_RendererInfo info;
    if (SDL_GetRendererInfo(renderer, &info) == 0 && info.name != NULL) {
        snprintf(driver, sizeof(driver), "%s", info.name);
    }

    int width = 0;
    int height = 0;
    SDL_GetWindowSize(window, &width, &height);

    FILE *marker = fopen(path, "w");
    if (marker == NULL) {
        return;
    }

    fprintf(marker,
        "{\"ok\":true,\"driver\":");
    json_escape(marker, driver);
    fprintf(marker, ",\"width\":%d,\"height\":%d}\n",
        width,
        height
    );
    fclose(marker);
}

int main(void) {
    if (!init_sdl()) {
        return 1;
    }

    AppWindow app = {0};
    if (!window_init(&app, WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT)) {
        shutdown_sdl();
        return 1;
    }

    SceneRenderer scene = {0};
    if (!renderer_load_background(&scene, app.renderer, BACKGROUND_IMAGE_PATH)) {
        window_destroy(&app);
        shutdown_sdl();
        return 1;
    }

    bool running = true;
    int presented_frames = 0;
    const int smoke_frames = parse_smoke_frames();
    while (running) {
        SDL_Event event;
        while (SDL_PollEvent(&event) == 1) {
            if (event.type == SDL_QUIT) {
                running = false;
            } else if (event.type == SDL_WINDOWEVENT &&
                       event.window.event == SDL_WINDOWEVENT_CLOSE) {
                running = false;
            }
        }

        renderer_draw_background(&scene, app.renderer, app.width, app.height);
        presented_frames++;
        if (presented_frames == 1) {
            write_ready_marker(app.window, app.renderer);
        }
        if (smoke_frames > 0 && presented_frames >= smoke_frames) {
            running = false;
            break;
        }
        SDL_Delay(16);
    }

    renderer_destroy(&scene);
    window_destroy(&app);
    shutdown_sdl();
    return 0;
}
