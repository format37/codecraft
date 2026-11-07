# The C++ engine: `make` builds build/codecraft-sim-cpp (g++, C++17, no dependency beyond the standard library).
# The viewer of the GUI mode (viewer/viewer.html) is built in as one string.
CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -ffp-contract=off
LDFLAGS ?= -pthread
SRC := $(wildcard src/*.cpp)
OBJ := $(patsubst src/%.cpp,build/obj/%.o,$(SRC)) build/obj/viewer_html.o

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

clean:
	rm -rf build

.PHONY: clean
