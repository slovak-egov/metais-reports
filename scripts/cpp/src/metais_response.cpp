#include "../include/metais_response.h"
#include "../include/json_utils.h"
#include <stdexcept>
#include <algorithm>

namespace metais {

    static std::string preview_body(const std::string& s, std::size_t max = 800) {
        if (s.size() <= max) return s;

        // show head + tail (helps when HTML error pages have useful footer)
        const std::size_t head = std::min<std::size_t>(max * 2 / 3, s.size());
        const std::size_t tail = std::min<std::size_t>(max - head, s.size() - head);

        std::string out;
        out.reserve(head + 64 + tail);
        out.append(s.data(), head);
        out.append("\n... [truncated, total bytes=" + std::to_string(s.size()) + "] ...\n");
        out.append(s.data() + (s.size() - tail), tail);
        return out;
    }

    static std::string preview_json_dump(const json& j, std::size_t max = 800) {
        std::string s;
        try { s = j.dump(2); } catch (...) { s = "<json dump failed>"; }
        return preview_body(s, max);
    }

    json parse_json_or_throw(const std::string& body, const std::string& tag) {
        try {
            return json::parse(body);
        } catch (const std::exception& e) {
            throw std::runtime_error(
                "[" + tag + "] Response was not valid JSON: " + std::string(e.what()) +
                "\nBody preview:\n" + preview_body(body)
            );
        }
    }

    json extract_results_array_or_throw(const json& j, const std::string& tag) {
        if (j.is_object() && j.contains("type") && j.contains("message")) {
            throw std::runtime_error(
                "[" + tag + "] API error object (preview):\n" + preview_json_dump(j)
            );
        }

        json arr = extract_result_array(j);
        if (arr.is_array()) return arr;

        throw std::runtime_error(
            "[" + tag + "] Could not extract result array from JSON.\nJSON preview:\n" +
            preview_json_dump(j)
        );
    }

    json parse_results_or_throw(const std::string& body, const std::string& tag) {
        json j = parse_json_or_throw(body, tag);
        return extract_results_array_or_throw(j, tag);
    }
}