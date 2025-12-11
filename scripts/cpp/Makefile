# Compiler + flags
CXX      := g++
CXXFLAGS := -std=c++17 -Wall -Wextra -O2 -Iinclude

# Linker flags (libs)
LDLIBS   := -lcurl

# Directories
SRC_DIR   := src
BUILD_DIR := build

# Binaries
MAIN_BIN      := metais_fetch

# Sources
MAIN_SRCS := $(SRC_DIR)/main.cpp \
             $(SRC_DIR)/fetch_http.cpp \
             $(SRC_DIR)/fetch_enums.cpp \
			 $(SRC_DIR)/fetch_metadata.cpp \
			 $(SRC_DIR)/fetch_http.cpp

# Objects (same .o reused where possible)
MAIN_OBJS       := $(patsubst $(SRC_DIR)/%.cpp,$(BUILD_DIR)/%.o,$(MAIN_SRCS))

.PHONY: all clean

# Default target
all: $(MAIN_BIN)

# Main orchestrator
$(MAIN_BIN): $(MAIN_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Test binary for fetch_http (using src/test.cpp)
$(TEST_FETCH_BIN): $(TEST_FETCH_OBJS)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Generic rule for compiling any .cpp in src/ to build/*.o
$(BUILD_DIR)/%.o: $(SRC_DIR)/%.cpp | $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# Ensure build/ exists
$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# Cleanup
clean:
	rm -rf $(BUILD_DIR) $(MAIN_BIN) $(TEST_FETCH_BIN)