#pragma once
#include "directory_layout.h"

namespace metais {
    // Pass 3b: Optional grid->sparse conversion for attributes.bin.
    // - Nodes: root/nodes/<citype>/attributes.bin
    // - Rels:  root/relations/<reltype>/attributes.bin
    // Writes attribute_offsets.bin when sparse chosen.
    // Updates format.json attributeLayout accordingly.
    void pass3_optimize_attributes(const DirectoryLayout& layout);
}