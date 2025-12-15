#pragma once
#include "binary_sink.h"

namespace metais {

    class NullSink : public BinarySink {
    public:
        void begin_page(long, int) override {}
        void write_item(const nlohmann::json&) override {}
        void end_page(std::size_t) override {}
    };

}