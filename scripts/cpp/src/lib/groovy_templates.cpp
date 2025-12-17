#include "groovy_templates.h"

#include <fstream>
#include <sstream>
#include <stdexcept>

namespace metais::groovy {

    std::string load_template_file(const std::filesystem::path& path) {
        std::ifstream in(path);
        if (!in.is_open()) {
            throw std::runtime_error("Cannot open groovy template: " + path.string());
        }
        std::ostringstream ss;
        ss << in.rdbuf();
        return ss.str();
    }

    std::string inject(std::string code, const std::string& needle, const std::string& value) {
        size_t pos = 0;
        while ((pos = code.find(needle, pos)) != std::string::npos) {
            code.replace(pos, needle.size(), value);
            pos += value.size();
        }
        return code;
    }

    std::string inject_limit_offset(std::string code, int limit, long offset) {
        code = inject(std::move(code), "__LIMIT__",  std::to_string(limit));
        code = inject(std::move(code), "__OFFSET__", std::to_string(offset));
        return code;
    }

}