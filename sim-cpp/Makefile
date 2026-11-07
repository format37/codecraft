# The C++ engine: `make` builds build/codecraft-sim-cpp (g++, C++17, no dependency beyond the standard library).
# The viewer of the GUI mode (viewer/viewer.html) is built in as one string.
CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -ffp-contract=off
LDFLAGS ?= -pthread
SRC := $(wildcard src/*.cpp)
OBJ := $(patsubst src/%.cpp,build/obj/%.o,$(SRC)) build/obj/viewer_html.o build/obj/version.o

# The engine's version, which `--version` and the viewer show: the version of its source, as the server computes it
# to tag the engine image (server/codecraft/builder.py, source_version): 12 hex digits of the sha256 of the
# `sha256sum` lines of the Dockerfile and the files it copies (SOURCES), in the order of their paths. The same
# version, the same engine. `make version` prints it.
SOURCES := Dockerfile Makefile src viewer
SHA256 := $(shell command -v sha256sum >/dev/null 2>&1 && echo sha256sum || echo shasum -a 256)
VERSION := $(shell find $(SOURCES) -type f | LC_ALL=C sort | xargs $(SHA256) | $(SHA256) | cut -c1-12)

build/codecraft-sim-cpp: $(OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

build/obj/%.o: src/%.cpp $(wildcard src/*.hpp)
	@mkdir -p build/obj
	$(CXX) $(CXXFLAGS) -c -o $@ $<

# a raw string literal: the page must not contain the closing delimiter )VIEWER"
build/viewer_html.cpp: viewer/viewer.html
	@mkdir -p build
	@if grep -q ')VIEWER"' $<; then echo "viewer.html contains )VIEWER\"" >&2; exit 1; fi
	{ printf 'extern const char VIEWER_HTML[];\nconst char VIEWER_HTML[] = R"VIEWER('; cat $<; \
	  printf ')VIEWER";\n'; } > $@

build/obj/viewer_html.o: build/viewer_html.cpp
	@mkdir -p build/obj
	$(CXX) $(CXXFLAGS) -c -o $@ $<

# written again only when the version changes, so that a build with nothing new relinks nothing
build/version.cpp: FORCE
	@mkdir -p build
	@printf 'extern const char ENGINE_VERSION[];\nconst char ENGINE_VERSION[] = "%s";\n' "$(VERSION)" > $@.new
	@if cmp -s $@.new $@; then rm $@.new; else mv $@.new $@; fi

build/obj/version.o: build/version.cpp
	@mkdir -p build/obj
	$(CXX) $(CXXFLAGS) -c -o $@ $<

version:
	@echo $(VERSION)

clean:
	rm -rf build

FORCE:

.PHONY: clean version FORCE
