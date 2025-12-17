#pragma once
#include <cstddef>
#include <iostream>
#include <string>
#include <chrono>

#if defined(__unix__) || defined(__APPLE__)
  #include <unistd.h>
  #include <sys/ioctl.h>
#endif

namespace metais {

    inline bool stderr_is_tty() {
    #if defined(__unix__) || defined(__APPLE__)
        return ::isatty(STDERR_FILENO);
    #else
        return false;
    #endif
    }

    inline std::size_t terminal_cols_stderr(std::size_t fallback = 80) {
    #if defined(__unix__) || defined(__APPLE__)
        if (!stderr_is_tty()) return fallback;
        winsize ws{};
        if (::ioctl(STDERR_FILENO, TIOCGWINSZ, &ws) == 0 && ws.ws_col > 0) {
            return static_cast<std::size_t>(ws.ws_col);
        }
    #endif
        return fallback;
    }

    struct ProgressBar {
        std::string label;
        std::size_t cur = 0;
        std::size_t total = 0;

        // throttle by time (prevents spam even if you call update() a lot)
        std::chrono::steady_clock::time_point last_emit = std::chrono::steady_clock::now();
        std::chrono::milliseconds min_interval{100};

        explicit ProgressBar(std::string lbl, std::size_t tot)
            : label(std::move(lbl)), total(tot) {}

        void update(std::size_t v) {
            cur = v;
            auto now = std::chrono::steady_clock::now();
            if (cur == total || now - last_emit >= min_interval) {
                last_emit = now;
                render();
            }
        }

        void render() const {
            if (total == 0) return;

            if (!stderr_is_tty()) {
                // non-tty: newline updates are safer
                if (cur == total) {
                    std::cerr << "[" << label << "] " << cur << "/" << total << "\n";
                }
                return;
            }

            const auto cols = terminal_cols_stderr(80);

            // Build prefix/suffix; compute bar width to fit current terminal cols
            const std::string prefix = "[" + label + "] [";
            const std::string suffix =
                "] " + std::to_string(cur) + "/" + std::to_string(total) +
                " (" + std::to_string(int((double(cur) / double(total)) * 100.0)) + "%)";

            // Reserve at least a tiny bar
            std::size_t bar_width = 10;
            if (cols > prefix.size() + suffix.size() + 1) {
                bar_width = cols - prefix.size() - suffix.size() - 1;
                if (bar_width < 10) bar_width = 10;
            }

            const double frac = double(cur) / double(total);
            const std::size_t filled = std::size_t(frac * double(bar_width));

            // \r to start of line, \033[2K clears whole line (no guessing!)
            std::cerr << "\r\033[2K" << prefix;
            for (std::size_t i = 0; i < bar_width; ++i) std::cerr << (i < filled ? "#" : ".");
            std::cerr << suffix;
            std::cerr.flush();
        }

        // Call before printing normal output
        void finish(bool clear = true) const {
            if (!stderr_is_tty()) return;

            if (clear) {
                std::cerr << "\r\033[2K"; // clear line
            } else {
                std::cerr << "\n";
            }
            std::cerr.flush();
        }
    };

}