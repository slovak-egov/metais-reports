## Overview

This C++ pipeline fetches large MetaIS datasets (enums, metadata, raw nodes and relations)
via HTTP/Groovy-based APIs and writes them as date-stamped, restartable outputs under
`output/<date>/`.

Key properties:
- idempotent, restartable runs via `.done` markers
- sharded NDJSON output for large datasets
- supports serial adaptive paging and parallel fixed-offset fetching
- designed to tolerate partial failures and resume safely

It is part of the larger `metais-reports` toolchain and is invoked as `metais_fetch`.

needs these tools to run:
- sudo apt-get install libcurl4-openssl-dev
- sudo apt install nlohmann-json3-dev