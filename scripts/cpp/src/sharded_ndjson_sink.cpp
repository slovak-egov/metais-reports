#include "../include/sharded_ndjson_sink.h"

#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace fs = std::filesystem;

namespace {

    static fs::path shard_path(
        const fs::path& dir,
        const std::string& base,
        long offset
    ) {
        std::ostringstream name;
        name << base << "."
            << std::setw(9) << std::setfill('0') << offset
            << ".ndjson";
        return dir / name.str();
    }

}

namespace metais {

    ShardedNdjsonSink::ShardedNdjsonSink(
        fs::path pages_dir,
        std::string base_name
    )
        : pages_dir_(std::move(pages_dir)),
        base_name_(std::move(base_name))
    {
        fs::create_directories(pages_dir_);
    }

    void ShardedNdjsonSink::begin_page(long offset, int /*limit*/) {
        current_offset_ = offset;

        final_path_ = shard_path(pages_dir_, base_name_, offset);
        tmp_path_   = final_path_;
        tmp_path_  += ".tmp";

        if (fs::exists(tmp_path_)) fs::remove(tmp_path_);

        // Idempotency: if page already exists, skip silently
        if (fs::exists(final_path_)) {
            current_offset_ = -1;
            return;
        }

        out_.open(tmp_path_, std::ios::binary);
        if (!out_) {
            throw std::runtime_error("Cannot open " + tmp_path_.string());
        }
    }

    void ShardedNdjsonSink::write_item(const nlohmann::json& obj) {
        if (current_offset_ < 0) return; // skipped page
        out_ << obj.dump() << "\n";
    }

    void ShardedNdjsonSink::end_page(std::size_t /*n*/) {
        if (current_offset_ < 0) return;

        out_.close();
        fs::rename(tmp_path_, final_path_);

        current_offset_ = -1;
    }

}
