#pragma once
#include <string>
#include <iostream>
#include "json_utils.h"

namespace metais {

struct PathsConfig {
    std::string output_root   = "output";
    std::string metadata_root = "metadata";
    std::string enums_root    = "enums";
    std::string codelists_root = "codelists";
    std::string nodes_root    = "nodes";
    std::string rels_root     = "relations";
    std::string packed_root   = "packed";
};

// Load config/paths.json if it exists, otherwise use defaults.
// If some keys are missing, defaults stay in place.
inline PathsConfig load_paths_config(const std::string& filepath) {
    PathsConfig cfg;

    try {
        json j = load_json_file(filepath);

        if (j.contains("output_root"))    cfg.output_root    = j["output_root"].get<std::string>();
        if (j.contains("metadata_root"))  cfg.metadata_root  = j["metadata_root"].get<std::string>();
        if (j.contains("enums_root"))     cfg.enums_root     = j["enums_root"].get<std::string>();
        if (j.contains("codelists_root")) cfg.codelists_root = j["codelists_root"].get<std::string>();
        if (j.contains("nodes_root"))     cfg.nodes_root     = j["nodes_root"].get<std::string>();
        if (j.contains("rels_root"))      cfg.rels_root      = j["rels_root"].get<std::string>();
        if (j.contains("packed_root"))    cfg.packed_root    = j["packed_root"].get<std::string>();

    } catch (const std::exception& e) {
        // Fallback: keep defaults, just warn
        std::cerr << "[paths_config] WARNING: " << e.what()
                  << " - using default paths.\n";
    }

    return cfg;
}

}