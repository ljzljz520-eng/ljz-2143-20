# 目标机构建首帧验证用的 C/SDL 二进制。
# 依赖（由 wdeliver 探测/依赖对比在安装前实测，不假设已装）：
#   make gcc libsdl2-dev(可选) libsdl2-image-dev(可选 PPM 路径不需要 image)
CC ?= gcc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra
SRC := src/main.c src/window.c src/renderer.c src/ppm.c
BIN := bin/visual-window-app

.PHONY: all clean

all: $(BIN)

$(BIN): $(SRC)
	@mkdir -p bin
	@# 优先使用 SDL2_image（PNG 路径）；缺库时退化为仅 SDL2（PPM 路径）
	@if pkg-config --exists SDL2_image 2>/dev/null; then \
	  echo "building with SDL2 + SDL2_image"; \
	  $(CC) $(CFLAGS) $(shell pkg-config --cflags SDL2_image) $(SRC) -o $@ \
	    $(shell pkg-config --libs SDL2_image) -lm; \
	else \
	  echo "SDL2_image not found; building SDL2-only (PPM background)"; \
	  $(CC) $(CFLAGS) -DWDELIVER_NO_SDL_IMAGE=1 $(SRC) -o $@ \
	    $(shell pkg-config --cflags --libs sdl2) -lm; \
	fi

clean:
	rm -rf bin
