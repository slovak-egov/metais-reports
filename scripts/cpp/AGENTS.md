# Repository Guidelines

## Ground rules
- Treat `docs/architecture.md` and `docs/architecture_summary.md` as the source of truth for contracts/invariants.
- If code disagrees with the doc, call it out explicitly instead of “fixing” it.

## Scope (IMPORTANT)
- This Codex session applies only to the C++ pipeline in `scripts/cpp/`.
- Ignore everything outside this directory unless explicitly requested.
- Treat `../../output/`, `build/`, generated `.bin`, `.ndjson`, and bulk JSON dumps as read-only artifacts.
- Do not infer behavior from generated outputs; always inspect source or config files.
- When making claims, always cite file paths and symbols. If unsure, search the codebase instead of guessing.

## Project Structure & Module Organization
- Core sources live in `src/` (orchestrator `main.cpp`, HTTP/paging logic in `fetch_*`, config loaders in
  `http_config.cpp`, layout helpers in `directory_layout` files).
- Public headers are in `include/`; mirror names when adding new modules.
- Runtime configuration sits in `config/json` (`paths.json` controls output roots such as `output/`, `metadata/`,
  `enums/`, `relations/`; `http_config.json` sets auth/paging; `URI.json` sets endpoints) and Groovy templates in
  `config/groovy`.
- Build artifacts land in `build/`; the `metais_fetch` binary is produced at repo root.
- Generated data and temp directories are created at runtime under the roots defined in `config/json/paths.json`.

## Build, Test, and Development Commands
- `make` — build `metais_fetch` with g++17 (`-Wall -Wextra -O2`, links `-lcurl`), writing objects to `build/`.
- `make clean` — remove `build/`, `metais_fetch`, and any test binaries built manually.
- Run locally with a token: `METAIS_TOKEN=... ./metais_fetch`; executes using configs in `config/json` and reports the resolved `project_root`/`cwd` before writing output.

## Coding Style & Naming Conventions
- C++17, 4-space indentation, brace-on-newline as in existing sources. Keep filenames and namespaces in `snake_case` and continue using the `metais` namespace for shared helpers. Place shared types in headers under `include/` and implementation in `src/` with matching names. JSON configuration keys are lower_snake_case; prefer extending existing files rather than creating ad-hoc ones.

## Testing Guidelines
- There is no dedicated test harness; sanity-check by running `./metais_fetch` with small paging (e.g., set `paging.max_pages` to a tiny value) and verifying the created directories under the configured output root. For new features, add lightweight integration checks alongside source (e.g., a `*_test.cpp` compiled with the same `CXXFLAGS` into `build/`) and keep them idempotent.

## Commit & Pull Request Guidelines
- Use imperative, descriptive commit messages (e.g., "add adaptive pager bounds"), and mention touched modules/configs. For PRs, include: a short summary, why the change is needed, configs to update (`config/json`/`config/params`), and sample run logs or output paths demonstrating success. Reference related tickets/issues when available.

## Security & Configuration Tips
- Auth expects `METAIS_TOKEN` with a Bearer prefix per `config/json/http_config.json`; avoid hardcoding or committing tokens. Keep endpoint/config changes explicit and documented, and reset paging/timeouts carefully to avoid over-fetching. Clean `build/`/outputs after experiments to avoid leaking data.
