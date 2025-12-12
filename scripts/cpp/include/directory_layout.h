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
        fs::path project_root;
        std::string dump_date;

        fs::path raw_root;
        fs::path date_root;

        // existing
        fs::path metadata_root;
        fs::path enums_root;
        fs::path nodes_meta_dir;
        fs::path rels_meta_dir;

        fs::path packed_root;
        fs::path dict_dir;
        fs::path nodes_packed;
        fs::path uuid_index_dir;
        fs::path uuid_types_dir;
        fs::path rels_packed;

        fs::path tmp_dir;

        // NEW: optional raw JSON dumps (unpacked)
        fs::path raw_nodes_dir;   // date_root / cfg.nodes_root
        fs::path raw_rels_dir;    // date_root / cfg.rels_root

        DirectoryLayout(const PathsConfig& cfg_,
                        const std::string& dump_date_,
                        const fs::path& project_root_)
            : cfg(cfg_),
            project_root(project_root_),
            dump_date(dump_date_)
        {
            raw_root  = project_root / cfg.output_root;
            date_root = raw_root / dump_date;

            metadata_root  = date_root / cfg.metadata_root;
            enums_root     = date_root / cfg.enums_root;

            nodes_meta_dir = metadata_root / cfg.nodes_root; // metadata/nodes
            rels_meta_dir  = metadata_root / cfg.rels_root;  // metadata/relations

            raw_nodes_dir  = date_root / cfg.nodes_root;     // DATE/nodes
            raw_rels_dir   = date_root / cfg.rels_root;      // DATE/relations

            packed_root    = date_root / cfg.packed_root;
            dict_dir       = packed_root / "dict";
            nodes_packed   = packed_root / "nodes";
            uuid_index_dir = packed_root / "uuid_index";
            uuid_types_dir = packed_root / "uuid_types";
            rels_packed    = packed_root / "relations";

            tmp_dir = date_root / "tmp";
        }

        void create_all(bool verbose = true) const {
            std::vector<fs::path> dirs = {
                metadata_root,
                enums_root,
                nodes_meta_dir,
                rels_meta_dir,

                // raw dumps (cheap to create even if you don't use them)
                raw_nodes_dir,
                raw_rels_dir,

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
                    throw std::runtime_error("Failed to create directory '" + d.string() + "': " + ec.message());
                }
                if (verbose) std::cout << "[mkdir] " << d << "\n";
            }
        }
    };

}