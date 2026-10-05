#ifndef PPM_H
#define PPM_H

#include <stdbool.h>
#include <SDL2/SDL.h>

/* 加载二进制 PPM(P6) 为 SDL 纹理。
 * 用于目标机缺少 SDL2_image 时的回退背景路径——系统组件是否齐全
 * 由服务端“依赖对比”和客户端探测结论决定，代码两条路都保留。 */
bool ppm_load_texture(SDL_Renderer *renderer, const char *path,
                      SDL_Texture **out);

/* 把当前渲染目标读回并写成 P6 PPM（首帧证据）。 */
bool ppm_save_framebuffer(SDL_Renderer *renderer, int width, int height,
                          const char *path);

#endif
