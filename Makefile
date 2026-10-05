CC := gcc
CFLAGS := -std=c11 -O2 -Wall -Wextra -Werror
SRC_DIR := src
TARGET := visual-window-app
SOURCES := $(SRC_DIR)/main.c $(SRC_DIR)/window.c $(SRC_DIR)/renderer.c

SDL_CFLAGS := $(shell sdl2-config --cflags)
SDL_LIBS := $(shell sdl2-config --libs)
LDLIBS := $(SDL_LIBS) -lSDL2_image

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) $(CFLAGS) $(SDL_CFLAGS) $(SOURCES) -o $@ $(LDLIBS)

run: $(TARGET)
	./$(TARGET)

clean:
	rm -f $(TARGET)


# ---- WDeliver 交付验证器（纯 Python 标准库） ----
WD_PY ?= python3

.PHONY: wdeliver-build wdeliver-server wdeliver-accept wdeliver-deps

wdeliver-build:
	$(WD_PY) -m wdeliver.builder.build --version 1.0.0
	$(WD_PY) -m wdeliver.builder.build --version 1.1.0

wdeliver-deps:
	mkdir -p /tmp/wd-extract && tar -xzf packages/visual-window-app_1.1.0.wdz -C /tmp/wd-extract
	$(WD_PY) -m wdeliver.builder.deps --payload /tmp/wd-extract 	  --package packages/visual-window-app_1.1.0.wdz

wdeliver-server:
	WDELIVER_TEST=1 $(WD_PY) -m wdeliver.server.app --port 8448

wdeliver-accept:
	$(WD_PY) tests/acceptance.py
