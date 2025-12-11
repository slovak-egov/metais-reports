#pragma once
#include <filesystem>
#include <string>

namespace metais {

namespace fs = std::filesystem;

// Find the nearest parent directory containing ".git".
// If none found, returns the starting directory.
inline fs::path find_project_root(const fs::path& start = fs::current_path())
{
    fs::path current = fs::absolute(start);

    // Walk upward until current == current.parent_path()
    while (true) {
        fs::path git_dir = current / ".git";

        if (fs::exists(git_dir)) {
            return current;
        }

        // Stop when we reach filesystem root
        fs::path parent = current.parent_path();
        if (parent == current) {
            // nothing found → fallback
            return start;
        }

        current = parent;
    }
}

}