# Compiler + flags
CXX      := g++
CXXFLAGS := -std=c++17 -Wall -Wextra -O2 -Iinclude
LDLIBS   := -lcurl

BUILD_DIR := build

FETCH_BIN  := metais_fetch
FETCH_MAIN := src/fetch/main.cpp

CONVERTER_BIN  := metais_converter
CONVERTER_MAIN := src/convert/main.cpp

LIB_SRCS := $(wildcard src/lib/*.cpp)
LIB_OBJS := $(patsubst src/lib/%.cpp,$(BUILD_DIR)/lib/%.o,$(LIB_SRCS))
FETCH_OBJ := $(BUILD_DIR)/fetch/main.o

CONVERTER_SRCS := $(wildcard src/convert/*.cpp)
CONVERTER_OBJS := $(patsubst src/convert/%.cpp,$(BUILD_DIR)/convert/%.o,$(CONVERTER_SRCS))

.PHONY: all clean

all: $(FETCH_BIN) $(CONVERTER_BIN)

$(FETCH_BIN): $(LIB_OBJS) $(FETCH_OBJ)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(CONVERTER_BIN): $(LIB_OBJS) $(CONVERTER_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Compile library sources
$(BUILD_DIR)/lib/%.o: src/lib/%.cpp | $(BUILD_DIR)/lib
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD_DIR)/convert/%.o: src/convert/%.cpp | $(BUILD_DIR)/convert
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Compile fetch main
$(BUILD_DIR)/fetch/main.o: src/fetch/main.cpp | $(BUILD_DIR)/fetch
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Create build dirs
$(BUILD_DIR)/lib:
	mkdir -p $@

$(BUILD_DIR)/fetch:
	mkdir -p $@

$(BUILD_DIR)/convert:
	mkdir -p $@

clean:
	rm -rf $(BUILD_DIR) $(FETCH_BIN) $(CONVERTER_BIN)