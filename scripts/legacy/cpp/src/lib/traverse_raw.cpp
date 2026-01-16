#include "traverse_raw.h"
#include "shard_naming.h"

#include <algorithm>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <iostream>
#include <string>

namespace metais {

    static bool starts_with(std::string_view s, std::string_view prefix) {
        return s.size() >= prefix.size() && s.substr(0, prefix.size()) == prefix;
    }

    std::optional<long> parse_offset_from_meta_filename(std::string_view fname,
                                                        std::string_view base) {
        const std::string prefix = std::string(base) + ".";
        const std::string suffix = ".meta.json";

        if (!starts_with(fname, prefix)) return std::nullopt;
        if (fname.size() != prefix.size() + kShardPad + suffix.size()) return std::nullopt;
        if (fname.substr(fname.size() - suffix.size()) != suffix) return std::nullopt;

        const auto digits = fname.substr(prefix.size(), kShardPad);
        for (char c : digits) if (c < '0' || c > '9') return std::nullopt;

        try {
            return std::stol(std::string(digits));
        } catch (...) {
            return std::nullopt;
        }
    }

    std::vector<ShardInfo> list_shards_by_meta(const std::filesystem::path& pages_dir, std::string_view base) {
        std::vector<ShardInfo> out;

        if (!std::filesystem::exists(pages_dir)) {
            throw std::runtime_error("Pages dir does not exist: " + pages_dir.string());
        }

        for (const auto& entry : std::filesystem::directory_iterator(pages_dir)) {
            if (!entry.is_regular_file()) continue;

            const auto fname = entry.path().filename().string();
            auto off = parse_offset_from_meta_filename(fname, base);
            if (!off) continue;

            ShardInfo s;
            s.offset = *off;
            s.meta_path = shard_meta_path(pages_dir, base, s.offset);
            s.ndjson_path = shard_data_path(pages_dir, base, s.offset);

            if (!std::filesystem::exists(s.ndjson_path)) {
                throw std::runtime_error("Found meta shard but missing data file: " + s.ndjson_path.string());
            }

            out.push_back(std::move(s));
        }

        std::sort(out.begin(), out.end(),
                [](const ShardInfo& a, const ShardInfo& b) { return a.offset < b.offset; });

        return out;
    }

    NdjsonJsonRange::NdjsonJsonRange(std::filesystem::path pages_dir, std::string base, bool skip_bad_json)
        : pages_dir_(std::move(pages_dir))
        , base_(std::move(base))
        , skip_bad_json_(skip_bad_json)
        , shards_(list_shards_by_meta(pages_dir_, base_))
    {}

    NdjsonJsonRange::iterator::iterator() = default;

    NdjsonJsonRange::iterator::iterator(const NdjsonJsonRange* owner)
        : owner_(owner)
    {
        if (!owner_ || owner_->shards_.empty()) {
            owner_ = nullptr;
            at_end_ = true;
            return;
        }
        shard_i_ = 0;
        open_current_shard_or_end();
        advance_or_end(); // position on first valid record
    }

    NdjsonJsonRange::iterator::reference NdjsonJsonRange::iterator::operator*() const { return cur_; }
    NdjsonJsonRange::iterator::pointer   NdjsonJsonRange::iterator::operator->() const { return &cur_; }

    NdjsonJsonRange::iterator& NdjsonJsonRange::iterator::operator++() {
        advance_or_end();
        return *this;
    }

    bool NdjsonJsonRange::iterator::operator==(const iterator& other) const {
        // If either is end, they're equal iff both are end
        const bool end_a = (owner_ == nullptr) || at_end_;
        const bool end_b = (other.owner_ == nullptr) || other.at_end_;
        if (end_a || end_b) return end_a == end_b;

        // Otherwise: same range and same internal position
        return owner_ == other.owner_
            && shard_i_ == other.shard_i_
            && line_no_ == other.line_no_;
    }

    bool NdjsonJsonRange::iterator::operator!=(const iterator& other) const { return !(*this == other); }

    void NdjsonJsonRange::iterator::open_current_shard_or_end() {
        while (owner_ && shard_i_ < owner_->shards_.size()) {
            const auto& shard = owner_->shards_[shard_i_];

            in_.close();
            in_.clear();
            in_.open(shard.ndjson_path, std::ios::binary);
            line_no_ = 0;

            if (!in_) {
                throw std::runtime_error("Cannot open ndjson: " + shard.ndjson_path.string());
            }
            return;
        }

        // end
        owner_ = nullptr;
        at_end_ = true;
    }

    void NdjsonJsonRange::iterator::advance_or_end() {
        if (!owner_ || at_end_) {
            owner_ = nullptr;
            at_end_ = true;

            return;
        }

        while (true) {
            if (!std::getline(in_, line_)) {
                // next shard
                ++shard_i_;
                if (shard_i_ >= owner_->shards_.size()) {
                    owner_ = nullptr;
                    at_end_ = true;
                    return;
                }
                open_current_shard_or_end();
                continue;
            }

            ++line_no_;
            if (line_.empty()) continue;

            const auto& shard = owner_->shards_[shard_i_];

            try {
                cur_.shard_index  = shard_i_;
                cur_.shard_count  = owner_->shards_.size();
                cur_.shard_offset = shard.offset;
                cur_.line_no      = line_no_;
                cur_.obj          = json::parse(line_.begin(), line_.end()); // no extra copy
                return;
            } catch (const std::exception& e) {
                const auto& shard = owner_->shards_[shard_i_];

                auto preview = line_;
                if (preview.size() > 400) preview.resize(400);

                throw std::runtime_error(
                    "[ndjson:" + owner_->base_ + "] invalid JSON line\n"
                    "  shard_index=" + std::to_string(shard_i_) + "/" + std::to_string(owner_->shards_.size()) + "\n"
                    "  shard_offset=" + std::to_string(shard.offset) + "\n"
                    "  line_no=" + std::to_string(line_no_) + "\n"
                    "  file=" + shard.ndjson_path.string() + "\n"
                    "  error=" + std::string(e.what()) + "\n"
                    "  line_preview=" + preview + "\n"
                );
            }
        }
    }

    NdjsonJsonRange::iterator NdjsonJsonRange::begin() const { return iterator(this); }
    NdjsonJsonRange::iterator NdjsonJsonRange::end()   const { return iterator(); }

}