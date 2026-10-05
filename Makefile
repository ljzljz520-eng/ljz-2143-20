CC ?= gcc
SOURCE_DATE_EPOCH ?= 1700000000
CFLAGS := -std=c11 -O2 -Wall -Wextra -Werror \
  -ffile-prefix-map=$(CURDIR)=. \
  -fdebug-prefix-map=$(CURDIR)=. \
  $(SDL_CFLAGS)
SRC_DIR := src
TARGET := visual-window-app
SOURCES := $(SRC_DIR)/main.c $(SRC_DIR)/window.c $(SRC_DIR)/renderer.c

SDL_CFLAGS := $(shell sdl2-config --cflags 2>/dev/null)
SDL_LIBS := $(shell sdl2-config --libs 2>/dev/null)
LDLIBS := $(SDL_LIBS) -lSDL2_image

.PHONY: all clean run

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) $(CFLAGS) $(SOURCES) -o $@ $(LDLIBS)

bin/$(TARGET): $(SOURCES)
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(SOURCES) -o $@ $(LDLIBS)

run: $(TARGET)
	./$(TARGET)

clean:
	rm -f $(TARGET)
	rm -rf bin
