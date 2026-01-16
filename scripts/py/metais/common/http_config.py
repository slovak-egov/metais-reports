from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union, Literal, List
import sys
import os
import getpass
import time
import copy

from .json_utils import load_json_file
from .project_root import find_project_root

AuthMode = Literal["none", "user_pass", "client_credentials", "report_endpoint"]
ClientAuthMethod = Literal["client_secret_basic", "client_secret_post"]


@dataclass(slots=True)
class HTTPAuthConfig:
    mode: AuthMode = "report_endpoint"
    interactive: bool = True

    # env var NAMES (not values)
    user_env: str = "METAIS_USER"
    pass_env: str = "METAIS_PASS"
    client_id_env: str = "METAIS_CLIENT_ID"
    client_secret_env: str = "METAIS_CLIENT_SECRET"

    # client-credentials options
    client_auth_method: ClientAuthMethod = "client_secret_basic"
    client_scope: Optional[str] = "openid"   # set None to omit entirely

    user_agent: str = "metais-py-fetcher/1.0"

    # bearer cache (filled by get_bearer_token)
    bearer_token: Optional[str] = None
    bearer_obtained_at: Optional[float] = None
    bearer_expires_in: Optional[float] = None
    bearer_scope: Optional[str] = None

    report_url_env: str = "METAIS_REPORT_EXEC_URL"


@dataclass(slots=True)
class HTTPTimeoutsConfig:
    connect_seconds: float = 10.0
    read_seconds: float = 60.0  # requests timeout=(connect, read)


@dataclass(slots=True)
class HTTPRetriesConfig:
    max_attempts: int = 5
    base_delay_ms: int = 500
    max_delay_ms: int = 8000
    jitter_ms: int = 250
    retry_http: List[int] = field(default_factory=lambda: [408, 429, 500, 502, 503, 504])


@dataclass(slots=True)
class HTTPPagingConfig:
    enabled: bool = True
    page_size: int = 1000
    max_pages: int = 100_000
    offset_param: str = "offset"
    limit_param: str = "limit"


@dataclass(slots=True)
class HTTPConfig:
    auth: HTTPAuthConfig = field(default_factory=HTTPAuthConfig)
    timeouts: HTTPTimeoutsConfig = field(default_factory=HTTPTimeoutsConfig)
    retries: HTTPRetriesConfig = field(default_factory=HTTPRetriesConfig)
    paging: HTTPPagingConfig = field(default_factory=HTTPPagingConfig)


@dataclass(slots=True)
class ResolvedAuth:
    mode: AuthMode

    # mode = user_pass
    username: Optional[str] = None
    password: Optional[str] = None

    # mode = client_credentials
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    client_auth_method: ClientAuthMethod = "client_secret_basic"
    client_scope: Optional[str] = "openid"


def _require_env_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not name:
        raise RuntimeError(f"[auth] {what} env-var NAME is empty in config.")
    return name

def _env_get(name: str) -> str:
    """Get env var value by name; empty if name is empty or missing."""
    if not name or not name.strip():
        return ""
    return os.environ.get(name, "") or ""

def get_report_execute_url(http_cfg: HTTPConfig) -> str:
    key = _require_env_name(http_cfg.auth.report_url_env, "report_url_env")
    url = (_env_get(key) or "").strip()
    if not url:
        raise RuntimeError(f"[report_endpoint] Missing env var {key} with report execute URL.")
    return url

def load_http_config(
    filepath: Optional[Union[str, Path]] = None,
    *,
    project_root: Optional[Path] = None,
    verbose: bool = True,
) -> HTTPConfig:
    """
    Load config/http_config.json (defaults if missing).
    Unknown keys are ignored.
    """
    cfg = HTTPConfig()

    try:
        if filepath is None:
            root = project_root or find_project_root()
            filepath = root / "config" / "http_config.json"
        else:
            filepath = Path(filepath)

        if not filepath.exists():
            if verbose:
                print(f"[http_config] using defaults (missing: {filepath})", file=sys.stderr)
            return cfg

        j = load_json_file(filepath)
        if not isinstance(j, dict):
            raise RuntimeError(f"http_config must be a JSON object: {filepath}")

        # --- auth ---
        a = j.get("auth", {})
        if isinstance(a, dict):
            mode = a.get("mode")
            if isinstance(mode, str):
                # legacy aliases
                if mode in ("oidc_userpass_pkce", "userpass"):
                    mode = "user_pass"
                if mode in ("none", "user_pass", "client_credentials", "report_endpoint"):
                    cfg.auth.mode = mode
                else:
                    if verbose:
                        print(f"[http_config] WARNING: unknown auth.mode={mode!r}; keeping default.", file=sys.stderr)

            inter = a.get("interactive")
            if isinstance(inter, bool):
                cfg.auth.interactive = inter

            for k in (
                "user_env",
                "pass_env",
                "client_id_env",
                "client_secret_env",
                "user_agent",
                "client_auth_method",
                "report_url_env",
            ):
                v = a.get(k)
                if isinstance(v, str) and v.strip():
                    setattr(cfg.auth, k, v)

            # client_scope can be string OR null
            if "client_scope" in a:
                cs = a.get("client_scope")
                if cs is None:
                    cfg.auth.client_scope = None
                elif isinstance(cs, str):
                    cfg.auth.client_scope = cs

        # --- timeouts ---
        t = j.get("timeouts", {})
        if isinstance(t, dict):
            cs = t.get("connect_seconds")
            rs = t.get("read_seconds") if "read_seconds" in t else t.get("total_seconds")  # legacy alias
            if isinstance(cs, (int, float)):
                cfg.timeouts.connect_seconds = float(cs)
            if isinstance(rs, (int, float)):
                cfg.timeouts.read_seconds = float(rs)

        # --- retries ---
        r = j.get("retries", {})
        if isinstance(r, dict):
            for k in ("max_attempts", "base_delay_ms", "max_delay_ms", "jitter_ms"):
                v = r.get(k)
                if isinstance(v, int):
                    setattr(cfg.retries, k, v)

            rh = r.get("retry_http")
            if isinstance(rh, list):
                cfg.retries.retry_http = [int(x) for x in rh if isinstance(x, int)]

        # --- paging ---
        p = j.get("paging", {})
        if isinstance(p, dict):
            en = p.get("enabled")
            if isinstance(en, bool):
                cfg.paging.enabled = en
            for k in ("page_size", "max_pages"):
                v = p.get(k)
                if isinstance(v, int):
                    setattr(cfg.paging, k, v)
            for k in ("offset_param", "limit_param"):
                v = p.get(k)
                if isinstance(v, str) and v.strip():
                    setattr(cfg.paging, k, v)

        if verbose:
            print(f"[http_config] loaded {filepath}", file=sys.stderr)

        return cfg

    except Exception as e:
        if verbose:
            print(f"[http_config] WARNING: {e} - using defaults.", file=sys.stderr)
        return HTTPConfig()

def resolve_auth_inputs(auth: HTTPAuthConfig, *, verbose: bool = True) -> ResolvedAuth:
    """
    Resolve credential inputs for the selected auth mode.

    - user_pass:
        - read env values from auth.user_env/auth.pass_env
        - if missing and interactive=True -> prompt
        - if missing and interactive=False -> fail
    - client_credentials:
        - must exist in env; fail if missing
    - none:
        - returns mode=none
    """
    mode = auth.mode

    if mode in ("none", "report_endpoint"):
        return ResolvedAuth(mode=mode)

    if mode == "user_pass":
        user_key = _require_env_name(auth.user_env, "user_env")
        pass_key = _require_env_name(auth.pass_env, "pass_env")

        username = (os.environ.get(user_key) or "").strip()
        password = os.environ.get(pass_key) or ""

        missing = []
        if not username: missing.append(user_key)
        if not password: missing.append(pass_key)

        if missing:
            if not auth.interactive:
                raise RuntimeError(
                    f"[auth] Missing required env vars for user_pass: {', '.join(missing)} "
                    f"(interactive=false)."
                )

            if not sys.stdin.isatty():
                raise RuntimeError(
                    "[auth] interactive=true but no TTY available (refusing to prompt). "
                    f"Missing: {', '.join(missing)}"
                )

            if not username:
                username = getpass.getpass("MetaIS username / email: ").strip()
            if not password:
                password = getpass.getpass("MetaIS password: ")

        if not username or not password:
            raise RuntimeError("[auth] Empty username/password after prompting.")

        if verbose:
            print(f"[auth] user_pass resolved (username from {user_key})", file=sys.stderr)

        return ResolvedAuth(mode="user_pass", username=username, password=password)

    if mode == "client_credentials":
        id_key = _require_env_name(auth.client_id_env, "client_id_env")
        sec_key = _require_env_name(auth.client_secret_env, "client_secret_env")

        client_id = (os.environ.get(id_key) or "").strip()
        client_secret = os.environ.get(sec_key) or ""

        if not client_id or not client_secret:
            missing = []
            if not client_id:
                missing.append(id_key)
            if not client_secret:
                missing.append(sec_key)
            raise RuntimeError(f"[auth] Missing required env vars for client_credentials: {', '.join(missing)}.")

        if verbose:
            print(f"[auth] client_credentials resolved (client_id from {id_key})", file=sys.stderr)

        return ResolvedAuth(
            mode="client_credentials",
            client_id=client_id,
            client_secret=client_secret,
            client_auth_method=auth.client_auth_method,
            client_scope=auth.client_scope,
        )

    raise RuntimeError(f"[auth] Unknown auth mode: {mode!r}")


def get_bearer_token(
    resolved: ResolvedAuth,
    http_cfg: "HTTPConfig",
    *,
    base: str,
    verbose: bool = True,
    verify_tls: bool = True,
) -> str:
    """
    Fetch a bearer token using the resolved auth inputs and store it in http_cfg.auth.
    Fail-fast: raises on auth errors.
    """
    if resolved.mode == "none":
        raise RuntimeError("[auth] get_bearer_token called but auth.mode is 'none'.")

    # Import here to avoid import-time coupling / circular deps
    from metais.auth.metais_auth import bearer_from_user_pass_plain, bearer_from_client_credentials

    timeout = float(http_cfg.timeouts.read_seconds)

    if resolved.mode == "user_pass":
        assert resolved.username is not None and resolved.password is not None
        tok = bearer_from_user_pass_plain(
            username=resolved.username,
            password=resolved.password,
            verbose=verbose,
            base=base,
            timeout=timeout,
            user_agent=http_cfg.auth.user_agent,
            verify_tls=verify_tls,
        )
        http_cfg.auth.bearer_token = tok
        http_cfg.auth.bearer_obtained_at = time.time()
        # user-pass helper returns only token; expires_in isn't captured in your public API
        return tok

    if resolved.mode == "client_credentials":
        assert resolved.client_id is not None and resolved.client_secret is not None
        tok = bearer_from_client_credentials(
            client_id=resolved.client_id,
            client_secret=resolved.client_secret,
            scope=resolved.client_scope,
            verbose=verbose,
            base=base,
            timeout=timeout,
            user_agent=http_cfg.auth.user_agent,
            verify_tls=verify_tls,
            auth_method=resolved.client_auth_method,
        )
        http_cfg.auth.bearer_token = tok
        http_cfg.auth.bearer_obtained_at = time.time()
        return tok

    raise RuntimeError(f"[auth] Unsupported resolved.mode={resolved.mode!r}")

def open_http_cfg(http_cfg: HTTPConfig) -> HTTPConfig:
    cfg = copy.deepcopy(http_cfg)
    cfg.auth.mode = "none"
    return cfg
