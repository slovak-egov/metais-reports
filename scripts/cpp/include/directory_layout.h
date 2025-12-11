#pragma once
#include <string>
#include <filesystem>
#include <vector>
#include <iostream>
#include <system_error>

#include "paths_config.h"

namespace metais {

namespace fs = std::filesystem;

struct DirectoryLayout {
    PathsConfig cfg;
    fs::path project_root;   // e.g. /home/.../metais-reports
    std::string dump_date;   // e.g. "11-12-2025"

    // Computed paths:
    fs::path raw_root;       // project_root / output_root
    fs::path date_root;      // raw_root / dump_date

    fs::path metadata_root;  // date_root / metadata_root
    fs::path enums_root;      // date_root / enums_root
    fs::path nodes_meta_dir; // metadata_root / nodes_root
    fs::path rels_meta_dir;  // metadata_root / rels_root

    fs::path packed_root;    // date_root / packed_root
    fs::path dict_dir;       // packed_root / "dict"
    fs::path nodes_packed;   // packed_root / "nodes"
    fs::path uuid_index_dir; // packed_root / "uuid_index"
    fs::path uuid_types_dir; // packed_root / "uuid_types"
    fs::path rels_packed;    // packed_root / "relations"

    fs::path tmp_dir;        // date_root / "tmp"

    DirectoryLayout(const PathsConfig& cfg_,
                    const std::string& dump_date_,
                    const fs::path& project_root_)
        : cfg(cfg_),
          project_root(project_root_),
          dump_date(dump_date_)
    {
        // Base roots
        raw_root      = project_root / cfg.output_root;
        date_root     = raw_root / dump_date;

        // Metadata hierarchy
        metadata_root = date_root / cfg.metadata_root;
        enums_root    = date_root / cfg.enums_root;
        nodes_meta_dir= metadata_root / cfg.nodes_root;
        rels_meta_dir = metadata_root / cfg.rels_root;

        // Packed hierarchy
        packed_root   = date_root / cfg.packed_root;
        dict_dir      = packed_root / "dict";
        nodes_packed  = packed_root / "nodes";
        uuid_index_dir= packed_root / "uuid_index";
        uuid_types_dir= packed_root / "uuid_types";
        rels_packed   = packed_root / "relations";

        // Temp
        tmp_dir       = date_root / "tmp";
    }

    // Create all needed directories, like Python did with mkdir(parents=True, exist_ok=True)
    void create_all(bool verbose = true) const {
        std::vector<fs::path> dirs = {
            metadata_root,
            enums_root,
            nodes_meta_dir,
            rels_meta_dir,
            packed_root,
            dict_dir,
            nodes_packed,
            uuid_index_dir,
            uuid_types_dir,
            rels_packed,
            tmp_dir
        };

        for (const auto& d : dirs) {
            std::error_code ec;
            fs::create_directories(d, ec);
            if (ec) {
                throw std::runtime_error(
                    "Failed to create directory '" + d.string() +
                    "': " + ec.message()
                );
            }
            if (verbose) {
                std::cout << "[mkdir] " << d << "\n";
            }
        }
    }
};

}