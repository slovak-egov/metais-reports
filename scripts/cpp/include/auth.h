#pragma once
#include <string>
#include <fstream>
#include <sstream>
#include <iostream>
#include <cstdlib>
#include <filesystem>
#include "http_config.h"

namespace metais {

    inline std::string read_file_trim(const std::filesystem::path& p) {
        std::ifstream in(p);
        if (!in.is_open()) return "";
        std::ostringstream ss;
        ss << in.rdbuf();
        std::string s = ss.str();
        while (!s.empty() && (s.back()=='\n' || s.back()=='\r' || s.back()==' ' || s.back()=='\t')) s.pop_back();
        while (!s.empty() && (s.front()==' ' || s.front()=='\t')) s.erase(s.begin());
        return s;
    }

    // Non-interactive: env -> file -> "" (if not required) or throw (if required)
    inline std::string resolve_bearer_token_noninteractive(const HTTPConfig& cfg) {
        const auto& a = cfg.auth;
        if (a.mode == "none") return "";

        // 1) env
        if (!a.env_var.empty()) {
            if (const char* v = std::getenv(a.env_var.c_str())) {
                std::string tok = v;
                if (!tok.empty()) return tok;
            }
        }

        // 2) file
        if (!a.token_file.empty()) {
            std::string tok = read_file_trim(a.token_file);
            if (!tok.empty()) return tok;
        }

        return "";
    }

    // Interactive fallback ONLY when needed (e.g. after 401/403)
    inline std::string prompt_bearer_token() {
        std::string tok;
        std::cerr << "[auth] Bearer token invalid/missing. Paste a token and press Enter:\n> ";
        std::getline(std::cin, tok);
        // trim
        while (!tok.empty() && (tok.back()=='\n' || tok.back()=='\r' || tok.back()==' ' || tok.back()=='\t')) tok.pop_back();
        while (!tok.empty() && (tok.front()==' ' || tok.front()=='\t')) tok.erase(tok.begin());
        return tok;
    }

}