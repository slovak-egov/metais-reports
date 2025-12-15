#pragma once

#include <string>
#include <fstream>
#include <sstream>
#include <iostream>
#include <cstdlib>
#include <filesystem>
#include "http_config.h"

namespace metais {

    std::string read_file_trim(const std::filesystem::path& p);

    // Non-interactive: env -> file -> "" (if not required) or throw (if required)
    std::string resolve_bearer_token_noninteractive(const HTTPConfig& cfg);

    // Interactive fallback ONLY when needed (e.g. after 401/403)
    std::string prompt_bearer_token();

}