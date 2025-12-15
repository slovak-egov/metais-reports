#pragma once
#include <filesystem>
#include <fstream>
#include <nlohmann/json.hpp>
#include <stdexcept>

namespace metais {

    class PageSink {
    public:
        virtual ~PageSink() = default;
        virtual void begin_page(long offset, int limit) = 0;
        virtual void write_item(const nlohmann::json& obj) = 0;
        virtual void end_page(std::size_t received) = 0;
    };
    
    class NullSink final : public PageSink {
    public:
        void begin_page(long, int) override {}
        void write_item(const nlohmann::json&) override {}
        void end_page(std::size_t) override {}
    };

    class NdjsonSink final : public PageSink {
    public:
        explicit NdjsonSink(std::filesystem::path out_path)
            : out_path_(std::move(out_path))
        {
            std::filesystem::create_directories(out_path_.parent_path());
            out_.open(out_path_, std::ios::binary | std::ios::app);
            if (!out_.is_open())
                throw std::runtime_error("Failed to open sink file: " + out_path_.string());
        }

        void begin_page(long, int) override {}
        void write_item(const nlohmann::json& obj) override {
            out_ << obj.dump() << "\n";
        }
        void end_page(std::size_t) override {
            out_.flush();
        }

    private:
        std::filesystem::path out_path_;
        std::ofstream out_;
    };

}