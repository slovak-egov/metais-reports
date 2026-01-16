#pragma once
#include <string>
#include <filesystem>

namespace metais {

    struct PagerPolicy {
        int   min_limit = 200;
        int   max_limit = 20000;

        // If request is fast, grow page size.
        double grow_if_under_seconds = 15.0;
        double grow_factor           = 1.30;

        // If request is slow, shrink page size.
        double shrink_if_over_seconds = 40.0;
        double shrink_factor          = 0.90;

        // If timeout-ish (HTTP 408/504 or curl timeout), shrink hard.
        double timeout_factor         = 0.50;

        // Optional: quantize limit to multiples (helps keep it stable).
        int quantize_step = 1; // e.g. 100 or 250
    };

    // Load from JSON file. If missing/unreadable -> returns defaults.
    // (You can flip this to "throw" if you prefer strict config.)
    PagerPolicy load_pager_policy(const std::filesystem::path& path);

}