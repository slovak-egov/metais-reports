#include "packed_bootstrap.h"
#include "step_marker.h"

#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <chrono>

namespace fs = std::filesystem;

namespace metais {

    static void write_atomic_text(const fs::path& final_path, const std::string& content) {
        fs::create_directories(final_path.parent_path());
        const fs::path tmp = final_path.string() + ".tmp";

        {
            std::ofstream os(tmp, std::ios::binary);
            if (!os) throw std::runtime_error("Failed to open tmp for write: " + tmp.string());
            os.write(content.data(), (std::streamsize)content.size());
            if (!os) throw std::runtime_error("Failed to write tmp: " + tmp.string());
        }

        std::error_code ec;
        fs::rename(tmp, final_path, ec);
        if (ec) {
            // On Windows rename can fail if target exists; do replace-style rename
            fs::remove(final_path, ec);
            ec.clear();
            fs::rename(tmp, final_path, ec);
            if (ec) throw std::runtime_error("Atomic rename failed to " + final_path.string() + ": " + ec.message());
        }
    }

    static std::string now_utc_like() {
        std::time_t t = std::time(nullptr);
        return std::string("ctime=") + std::to_string((long long)t);
    }

    void bootstrap_packed_root(const DirectoryLayout& layout) {
        layout.create_convert_dirs(false);

        const std::string meta_list =
            R"(["owner","state","createdBy","createdAt","lastModifiedBy","lastModifiedAt"])";

        write_atomic_text(layout.nodes_packed / "metaAttributes.json", meta_list);
        write_atomic_text(layout.rels_packed  / "metaAttributes.json", meta_list);

        mark_done(layout.packed_root, ".pass0.done", "pass=0\n" + now_utc_like());
    }

}