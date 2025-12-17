#pragma once
#include <string>
#include <filesystem>

namespace metais::groovy {

    std::string load_template_file(const std::filesystem::path& path);

    std::string inject_limit_offset(std::string code, int limit, long offset);

    // injecting placeholders (__LIMIT__)
    std::string inject(std::string code, const std::string& needle, const std::string& value);

}