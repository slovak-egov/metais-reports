#pragma once

#include <string>
#include <unordered_map>
#include <unordered_set>
#include <cstdint>
#include <vector>
#include <algorithm>
#include <stdexcept>

namespace metais {

    struct AttributeCatalog {
        // (ci/rel)type -> set(attribute technicalName)
        std::unordered_map<std::string, std::unordered_set<std::string>> seen_attrs_by_type;

        // (ci/rel)type -> number of objects processed
        std::unordered_map<std::string, std::uint64_t> object_count_by_type;

        void note_object(const std::string& type) { ++object_count_by_type[type]; }
        void note_attr(const std::string& type, const std::string& tech) { seen_attrs_by_type[type].insert(tech); }
    };

    struct ValueDictionary {
        // Prepass collection (fast membership)
        std::unordered_set<std::string> seen;

        // Finalized storage (index -> value)
        std::vector<std::string> values;

        // Finalized lookup (value -> index)
        std::unordered_map<std::string, std::uint32_t> index_of;

        bool finalized = false;

        std::uint64_t total_seen = 0;

        // Call during prepass
        void note(std::string v) {
            if (finalized) throw std::logic_error("ValueDictionary::note after finalize()");
            seen.insert(std::move(v)); total_seen++;
        }

        // Call once after prepass
        void finalize_sorted() {
            if (finalized) return;

            std::cerr << "[dict] finalize: unique=" << seen.size()
                    << ", total_seen=" << total_seen << "\n";

            values.assign(seen.begin(), seen.end());
            std::sort(values.begin(), values.end());

            index_of.reserve(values.size());
            for (std::uint32_t i = 0; i < values.size(); ++i) index_of.emplace(values[i], i);

            finalized = true;
            std::unordered_set<std::string>().swap(seen);

            std::cerr << "[dict] finalize: values=" << values.size()
                    << ", index_of=" << index_of.size() << "\n";
        }

        std::uint32_t get_index(const std::string& v) const {
            auto it = index_of.find(v);
            if (it == index_of.end()) throw std::runtime_error("Value not found in dictionary");
            return it->second;
        }
    };

}