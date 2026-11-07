# The C++ engine: `make` builds build/codecraft-sim-cpp (g++, C++17, no dependency beyond the standard library).
CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -Wall -Wextra -ffp-contract=off
LDFLAGS ?= -pthread
SRC := $(wildcard src/*.cpp)
OBJ := $(patsubst src/%.cpp,build/obj/%.o,$(SRC))

build/codecraft-sim-cpp: $(OBJ)
	$(CXX) $(CXXFLAGS) -o $@ $^ $(LDFLAGS)

build/obj/%.o: src/%.cpp $(wildcard src/*.hpp)
	@mkdir -p build/obj
	$(CXX) $(CXXFLAGS) -c -o $@ $<

clean:
	rm -rf build

.PHONY: clean
