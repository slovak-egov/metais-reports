#pragma once
#include "directory_layout.h"
#include "prepass.h"

namespace metais {

    void freeze_schema_and_build_resolvers(const DirectoryLayout& layout, PrepassResult& pre);
    
}