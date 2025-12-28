#pragma once
#include "binary_formats.h"
#include <unordered_map>
#include <string_view>
#include <vector>
#include <string>
#include <fstream>

namespace metais {

    struct SvHash {
        using is_transparent = void;
        std::size_t operator()(std::string_view s) const noexcept {
            // FNV-1a 64-bit
            std::uint64_t h = 1469598103934665603ull;
            for (unsigned char c : s) {
                h ^= c;
                h *= 1099511628211ull;
            }
            return (std::size_t)h;
        }
    };
    struct SvEq {
        using is_transparent = void;
        bool operator()(std::string_view a, std::string_view b) const noexcept { return a == b; }
    };

    class DictLookup {
    public:
        void load(const fs::path& dict_dir) {
            // read dict.bin into blob_
            {
                std::ifstream is(dict_dir / "dict.bin", std::ios::binary);
                if (!is) throw std::runtime_error("DictLookup: open dict.bin failed");
                is.seekg(0, std::ios::end);
                std::size_t n = (std::size_t)is.tellg();
                is.seekg(0, std::ios::beg);
                blob_.resize(n);
                is.read(blob_.data(), (std::streamsize)n);
                if (!is) throw std::runtime_error("DictLookup: read dict.bin failed");
            }

            // read offsets
            {
                std::ifstream is(dict_dir / "dict.offsets.bin", std::ios::binary);
                if (!is) throw std::runtime_error("DictLookup: open dict.offsets.bin failed");
                is.seekg(0, std::ios::end);
                std::size_t bytes = (std::size_t)is.tellg();
                is.seekg(0, std::ios::beg);
                if (bytes % 8 != 0) throw std::runtime_error("DictLookup: offsets size not multiple of 8");
                std::size_t n64 = bytes / 8;
                offsets_.resize(n64);
                for (std::size_t i = 0; i < n64; ++i) offsets_[i] = read_u64_le(is);
            }

            if (offsets_.size() < 2) throw std::runtime_error("DictLookup: offsets too small");

            // build map: index -> slice
            map_.clear();
            map_.reserve(offsets_.size() - 1);

            for (std::size_t i = 0; i + 1 < offsets_.size(); ++i) {
                std::uint64_t a = offsets_[i];
                std::uint64_t b = offsets_[i + 1];
                if (b < a || b > blob_.size()) throw std::runtime_error("DictLookup: bad offsets");
                std::string_view sv(blob_.data() + (std::size_t)a, (std::size_t)(b - a));
                map_.emplace(sv, (DictIndex)i);
            }
        }

        DictIndex find_or_throw(std::string_view canonical_json_literal) const {
            auto it = map_.find(canonical_json_literal);
            if (it == map_.end()) throw std::runtime_error("DictLookup: value not in dictionary");
            return it->second;
        }

        bool try_find(std::string_view canonical_json_literal, DictIndex& out) const {
            auto it = map_.find(canonical_json_literal);
            if (it == map_.end()) return false;
            out = it->second;
            return true;
        }

    private:
        std::string blob_;
        std::vector<std::uint64_t> offsets_;
        std::unordered_map<std::string_view, DictIndex, SvHash, SvEq> map_;
    };

}