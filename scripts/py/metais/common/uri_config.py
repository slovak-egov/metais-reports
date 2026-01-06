from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union
import os
import sys

from .json_utils import load_json_file
from .project_root import find_project_root


def replace_all(s: str, frm: str, to: str) -> str:
    return s.replace(frm, to) if frm else s


def join_base_and_path(base: str, path: str) -> str:
    if not path:
        return base
    if not base:
        return path
    if base.endswith("/") and path.startswith("/"):
        return base + path[1:]
    if (not base.endswith("/")) and (not path.startswith("/")):
        return base + "/" + path
    return base + path


def resolve_base_url(instance_raw: str) -> str:
    # allow env to override everything
    env_url = os.environ.get("METAIS_BASE_URL")
    if env_url:
        return env_url

    instance = os.environ.get("METAIS_INSTANCE", instance_raw)

    if instance == "test":
        return "https://metais-test.slovensko.sk"
    return "https://metais.slovensko.sk"


@dataclass(slots=True)
class URIConfig:
    meta_instance: str = "prod"
    base_url: str = "https://metais.slovensko.sk"

    # paths (no host)
    enum_list_path: str = "api/enums-repo/enums/list"
    enum_detail_path_tpl: str = "api/enums-repo/enums/enum/valid/{name}"

    codelist_headers_list_path: str = (
        "api/codelist-repo/codelists/codelistheaders?language=sk&pageNumber=1&perPage=1000"
    )
    codelist_items_path_tpl: str = (
        "api/codelist-repo/codelists/codelistheaders/{name}/codelistitems?language=sk&pageNumber=1&perPage=10000"
    )

    citype_list_path: str = "api/types-repo/citypes/list"
    citype_detail_path_tpl: str = "api/types-repo/citypes/citype/{name}"

    reltype_list_path: str = "api/types-repo/relationshiptypes/list"
    reltype_detail_path_tpl: str = "api/types-repo/relationshiptypes/relationshiptype/{name}"

    report_run_path: str = "api/report/reports/run?lang=sk"

    # ---- full URL helpers ----
    def enum_list_url(self) -> str:
        return join_base_and_path(self.base_url, self.enum_list_path)

    def enum_detail_url(self, name: str) -> str:
        return join_base_and_path(self.base_url, replace_all(self.enum_detail_path_tpl, "{name}", name))

    def enum_detail_url_tpl(self) -> str:
        return join_base_and_path(self.base_url, self.enum_detail_path_tpl)

    def citype_list_url(self) -> str:
        return join_base_and_path(self.base_url, self.citype_list_path)

    def citype_detail_url(self, name: str) -> str:
        return join_base_and_path(self.base_url, replace_all(self.citype_detail_path_tpl, "{name}", name))

    def citype_detail_url_tpl(self) -> str:
        return join_base_and_path(self.base_url, self.citype_detail_path_tpl)

    def reltype_list_url(self) -> str:
        return join_base_and_path(self.base_url, self.reltype_list_path)

    def reltype_detail_url(self, name: str) -> str:
        return join_base_and_path(self.base_url, replace_all(self.reltype_detail_path_tpl, "{name}", name))

    def reltype_detail_url_tpl(self) -> str:
        return join_base_and_path(self.base_url, self.reltype_detail_path_tpl)

    def codelist_headers_list_url(self) -> str:
        return join_base_and_path(self.base_url, self.codelist_headers_list_path)

    def codelist_items_url(self, code: str) -> str:
        return join_base_and_path(self.base_url, replace_all(self.codelist_items_path_tpl, "{name}", code))

    def codelist_items_url_tpl(self) -> str:
        return join_base_and_path(self.base_url, self.codelist_items_path_tpl)

    def report_run_url(self) -> str:
        return join_base_and_path(self.base_url, self.report_run_path)


def load_uri_config(
    filepath: Optional[Union[str, Path]] = None,
    *,
    project_root: Optional[Path] = None,
    verbose: bool = True
) -> URIConfig:
    """
    Load config/URI.json if present; otherwise defaults.
    Then resolve base_url using METAIS_BASE_URL / METAIS_INSTANCE / meta-instance.
    """
    cfg = URIConfig()

    try:
        if filepath is None:
            root = project_root or find_project_root()
            filepath = root / "config" / "URI.json"
        else:
            filepath = Path(filepath)

        if filepath.exists():
            j = load_json_file(filepath)
            if not isinstance(j, dict):
                raise RuntimeError(f"URI config must be a JSON object: {filepath}")

            mi = j.get("meta-instance")
            if isinstance(mi, str):
                cfg.meta_instance = mi

            # override individual paths if present
            def set_if_str(attr: str, key: str) -> None:
                v = j.get(key)
                if isinstance(v, str):
                    setattr(cfg, attr, v)

            set_if_str("enum_list_path", "enum_list")
            set_if_str("enum_detail_path_tpl", "enum_detail")

            set_if_str("citype_list_path", "citype_list")
            set_if_str("citype_detail_path_tpl", "citype_detail")

            set_if_str("reltype_list_path", "reltype_list")
            set_if_str("reltype_detail_path_tpl", "reltype_detail")

            set_if_str("codelist_headers_list_path", "codelist_headers_list")
            set_if_str("codelist_items_path_tpl", "codelist_items")

            # legacy key name
            set_if_str("report_run_path", "apiuri")

    except Exception as e:
        print(f"[uri_config] WARNING: {e} - using default URIs.", file=sys.stderr)

    cfg.base_url = resolve_base_url(cfg.meta_instance)

    if verbose:
        print("[uri_config] instance =", cfg.meta_instance)
        print("[uri_config] base_url =", cfg.base_url)

    return cfg


# Optional: templates (your groovy paths)
@dataclass(slots=True)
class TemplateConfig:
    node_template_all: str
    node_template_valid_only: str
    rel_template_all: str
    rel_template_valid_only: str


def load_template_config(project_root: Optional[Path] = None) -> TemplateConfig:
    """
    Template paths (env override, otherwise defaults).
    Defaults are relative to project root (so they work regardless of cwd).
    """
    if project_root is None:
        project_root = find_project_root()

    def p(env_key: str, default_rel: str) -> str:
        v = os.environ.get(env_key)
        if v:
            return v
        return str(project_root / default_rel)

    return TemplateConfig(
        node_template_all=p("METAIS_NODE_TEMPLATE_ALL", "groovy/template/node.groovy"),
        node_template_valid_only=p("METAIS_NODE_TEMPLATE_VALID_ONLY", "groovy/template/node_safe.groovy"),
        rel_template_all=p("METAIS_REL_TEMPLATE_ALL", "groovy/template/relation.groovy"),
        rel_template_valid_only=p("METAIS_REL_TEMPLATE_VALID_ONLY", "groovy/template/relation_safe.groovy"),
    )