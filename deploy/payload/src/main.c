#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <SDL2/SDL.h>
#ifndef WDELIVER_NO_SDL_IMAGE
#include <SDL2/SDL_image.h>
#endif

#include "renderer.h"
#include "window.h"
#include "ppm.h"

#define WINDOW_TITLE "Visual Window App"
#define WINDOW_WIDTH 1280
#define WINDOW_HEIGHT 720

/* 背景查找顺序：环境指定 PPM -> 随包 PPM（免 SDL2_image 回退）
 * -> PNG（需要系统 libSDL2_image）。系统组件是否可用由探测/依赖对比
 * 在部署前给结论，这里不假定任何一条一定存在。 */
static bool load_background(AppWindow *app, SceneRenderer *scene) {
    const char *ppm_env = getenv("WD_BACKGROUND_PPM");
    const char *candidates[3] = {0};
    if (ppm_env != NULL && ppm_env[0] != '\0') {
        candidates[0] = ppm_env;
    } else {
        candidates[0] = "assets/background.ppm";
    }
#ifndef WDELIVER_NO_SDL_IMAGE
    candidates[1] = "assets/background.png";
#else
    /* SDL2_image 未编译进来：PNG 候选置空，只走 PPM。 */
    candidates[1] = NULL;
#endif
    candidates[2] = NULL;

    for (int i = 0; candidates[i] != NULL; ++i) {
        const char *p = candidates[i];
        if (strstr(p, ".ppm") != NULL) {
            SDL_Texture *tex = NULL;
            if (ppm_load_texture(app->renderer, p, &tex)) {
                scene->background = tex;
                fprintf(stderr, "background: loaded PPM %s\n", p);
                return true;
            }
        } else {
#ifndef WDELIVER_NO_SDL_IMAGE
            scene->background = IMG_LoadTexture(app->renderer, p);
            if (scene->background != NULL) {
                fprintf(stderr, "background: loaded via SDL2_image %s\n", p);
                return true;
            }
#endif
        }
    }
    fprintf(stderr, "background: no usable image (PPM%s) found\n",
#ifndef WDELIVER_NO_SDL_IMAGE
            " or PNG"
#else
            " only"
#endif
    );
    return false;
}

static bool init_sdl(void) {
    if (SDL_Init(SDL_INIT_VIDEO) != 0) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return false;
    }
#ifndef WDELIVER_NO_SDL_IMAGE
    int img_flags = IMG_INIT_PNG | IMG_INIT_JPG;
    int initted = IMG_Init(img_flags);
    if ((initted & img_flags) == 0) {
        /* PNG 路径不可用时不直接退出：可能仍有随包 PPM 背景。 */
        fprintf(stderr, "note: IMG_Init unavailable (%s); PPM fallback only\n",
                IMG_GetError());
    }
#else
    fprintf(stderr, "note: built without SDL2_image; PPM background only\n");
#endif
    return true;
}

static void shutdown_sdl(void) {
#ifndef WDELIVER_NO_SDL_IMAGE
    IMG_Quit();
#endif
    SDL_Quit();
}

static const char *renderer_name(SDL_Renderer *renderer) {
    SDL_RendererInfo info;
    if (SDL_GetRendererInfo(renderer, &info) == 0) {
        return info.name;
    }
    return "unknown";
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
    if (!load_background(&app, &scene)) {
        window_destroy(&app);
        shutdown_sdl();
        return 1;
    }

    /* 首帧验证模式：由 hooks/firstframe.py 驱动。
     * 渲染一帧 -> readback 写 PPM -> 写 hint receipt -> 退出。
     * 该模式下无需事件循环，用于自动化交付验证。 */
    const char *oneshot = getenv("WD_ONESHOT");
    if (oneshot != NULL && strcmp(oneshot, "1") == 0) {
        const char *frame_file = getenv("WD_FRAME_FILE");
        const char *receipt_file = getenv("WD_RECEIPT_FILE");
        SDL_SetRenderDrawColor(app.renderer, 0, 0, 0, 255);
        SDL_RenderClear(app.renderer);
        if (scene.background != NULL) {
            SDL_Rect dst = {0, 0, app.width, app.height};
            SDL_RenderCopy(app.renderer, scene.background, NULL, &dst);
        }
        SDL_RenderPresent(app);
        int rc = 4;
        if (frame_file != NULL &&
            ppm_save_framebuffer(app.renderer, app.width, app.height,
                                 frame_file)) {
            rc = 0;
            if (receipt_file != NULL) {
                FILE *rf = fopen(receipt_file, "w");
                if (rf != NULL) {
                    fprintf(rf,
                            "{\"frame_size\":[%d,%d],\"renderer\":\"%s\"}\n",
                            app.width, app.height, renderer_name(app.renderer));
                    fclose(rf);
                }
            }
        } else {
            fprintf(stderr, "oneshot: RenderReadPixels/PPM write failed\n");
        }
        renderer_destroy(&scene);
        window_destroy(&app);
        shutdown_sdl();
        return rc;
    }

    bool running = true;
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
        SDL_Delay(16);
    }

    renderer_destroy(&scene);
    window_destroy(&app);
    shutdown_sdl();
    return 0;
}
