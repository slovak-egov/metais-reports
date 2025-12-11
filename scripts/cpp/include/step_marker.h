#pragma once
#include <filesystem>
#include <fstream>
#include <string>
#include <system_error>
#include <stdexcept>
#include <iostream>

namespace metais {

namespace fs = std::filesystem;

// Return the path to the ".done" marker inside a directory.
inline fs::path done_marker_path(const fs::path& dir,
                                 const std::string& marker_name = ".done") {
    return dir / marker_name;
}

// Check if the ".done" marker exists in a directory.
inline bool is_done(const fs::path& dir,
                    const std::string& marker_name = ".done") {
    return fs::exists(done_marker_path(dir, marker_name));
}

// Create or update the ".done" marker in a directory.
// Optionally write a small message (e.g. timestamp, description) into it.
inline void mark_done(const fs::path& dir,
                      const std::string& marker_name = ".done",
                      const std::string& message = "") {
    std::error_code ec;
    fs::create_directories(dir, ec);
    if (ec) {
        throw std::runtime_error(
            "Failed to create directory '" + dir.string() +
            "' for done marker: " + ec.message()
        );
    }

    fs::path marker = done_marker_path(dir, marker_name);
    std::ofstream out(marker, std::ios::trunc);
    if (!out.is_open()) {
        throw std::runtime_error(
            "Failed to write done marker at '" + marker.string() + "'"
        );
    }

    if (!message.empty()) {
        out << message << "\n";
    }

    // flush + close by destructor
}

// Remove the ".done" marker if present.
inline void clear_done(const fs::path& dir,
                       const std::string& marker_name = ".done") {
    fs::path marker = done_marker_path(dir, marker_name);
    std::error_code ec;
    fs::remove(marker, ec);
    if (ec && ec.value() != static_cast<int>(std::errc::no_such_file_or_directory)) {
        std::cerr << "[step_marker] WARNING: Failed to remove "
                  << marker << ": " << ec.message() << "\n";
    }
}

}