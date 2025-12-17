#pragma once

#include <string>
#include <fstream>
#include <sstream>
#include <iostream>
#include <cstdlib>
#include <filesystem>

#include "http_config.h"
#include "http_response.h"

namespace metais {
    
    enum class AuthDecision {
        Retry,      // token updated (or should be), retry request
        FailHard,   // don’t retry (non-interactive / forbidden / etc.)
        Ignore      // not an auth issue
    };

    // Decide what to do on 401/403 (and optionally 0 w/ certain curl errors if you want).
    // Mutates bearer_token if it obtains a new one.
    AuthDecision handle_auth_challenge(
        const HTTPConfig& cfg,
        const HttpResponse& r,
        std::string& bearer_token,
        bool interactive_allowed
    );

    std::string read_file_trim(const std::filesystem::path& p);

    // Non-interactive: env -> file -> "" (if not required) or throw (if required)
    std::string resolve_bearer_token_noninteractive(const HTTPConfig& cfg);

    // Interactive fallback ONLY when needed (e.g. after 401/403)
    std::string prompt_bearer_token();

}