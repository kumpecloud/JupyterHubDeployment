"""JupyterHub config driven entirely by environment variables.

Bake this file into the image; override behavior at deploy time with .env.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

c = get_config()  # noqa: F821

log = logging.getLogger("jupyterhub")

_WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_workspace_scopes_lock = threading.Lock()
_workspace_scopes: list[str] = []
_m2m_token: str | None = None
_m2m_token_expires_at = 0.0


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if value is None or value == "":
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_set(name: str) -> set[str]:
    value = env(name, "") or ""
    return {item.strip() for item in value.split(",") if item.strip()}


def env_int(name: str, default: int) -> int:
    value = env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
c.JupyterHub.bind_url = env("JUPYTERHUB_BIND_URL", "http://:8000")
c.JupyterHub.hub_ip = env("JUPYTERHUB_HUB_IP", "0.0.0.0")
c.JupyterHub.hub_connect_ip = env("JUPYTERHUB_HUB_CONNECT_IP", "jupyterhub")
c.JupyterHub.db_url = env("JUPYTERHUB_DB_URL", "sqlite:////data/jupyterhub.sqlite")
c.JupyterHub.cookie_secret_file = env(
    "JUPYTERHUB_COOKIE_SECRET_FILE", "/data/jupyterhub_cookie_secret"
)

crypt_key = env("JUPYTERHUB_CRYPT_KEY")
if crypt_key:
    c.CryptKeeper.keys = [crypt_key]

admin_users = env_set("JUPYTERHUB_ADMIN_USERS")
if admin_users:
    c.Authenticator.admin_users = admin_users

allowed_users = env_set("JUPYTERHUB_ALLOWED_USERS")
if allowed_users:
    c.Authenticator.allowed_users = allowed_users
else:
    c.Authenticator.allow_all = True

# ---------------------------------------------------------------------------
# Idle culling (stop unused servers; optional purge inactive Hub users)
# ---------------------------------------------------------------------------
# IDLE_CULL_TIMEOUT_SECONDS: stop a running server after this much inactivity
# INACTIVE_USER_CULL_DAYS: if > 0, delete Hub users idle this many days (re-login OK;
#   personal ./data/users/<name> is kept; shared workspaces are never deleted)
services: list[dict[str, Any]] = []
roles: list[dict[str, Any]] = []

idle_cull_enabled = env_bool("IDLE_CULL_ENABLED", True)
idle_cull_timeout = env_int("IDLE_CULL_TIMEOUT_SECONDS", 3600)
idle_cull_every = env_int("IDLE_CULL_EVERY_SECONDS", max(idle_cull_timeout // 2, 60))
inactive_user_cull_days = env_int("INACTIVE_USER_CULL_DAYS", 0)

if idle_cull_enabled and idle_cull_timeout > 0:
    services.append(
        {
            "name": "idle-culler",
            "command": [
                sys.executable,
                "-m",
                "jupyterhub_idle_culler",
                f"--timeout={idle_cull_timeout}",
                f"--cull-every={idle_cull_every}",
            ],
        }
    )
    roles.append(
        {
            "name": "idle-culler",
            "scopes": [
                "list:users",
                "read:users:activity",
                "read:servers",
                "delete:servers",
            ],
            "services": ["idle-culler"],
        }
    )

if inactive_user_cull_days > 0:
    inactive_timeout = inactive_user_cull_days * 24 * 60 * 60
    inactive_every = env_int(
        "INACTIVE_USER_CULL_EVERY_SECONDS",
        max(inactive_timeout // 24, 3600),
    )
    services.append(
        {
            "name": "inactive-user-culler",
            "command": [
                sys.executable,
                "-m",
                "jupyterhub_idle_culler",
                f"--timeout={inactive_timeout}",
                f"--cull-every={inactive_every}",
                "--cull-users=True",
            ],
        }
    )
    roles.append(
        {
            "name": "inactive-user-culler",
            "scopes": [
                "list:users",
                "read:users:activity",
                "read:servers",
                "delete:servers",
                "admin:users",
            ],
            "services": ["inactive-user-culler"],
        }
    )

if services:
    c.JupyterHub.services = services
if roles:
    c.JupyterHub.load_roles = roles

# ---------------------------------------------------------------------------
# Spawner (Docker)
# ---------------------------------------------------------------------------
c.JupyterHub.spawner_class = "dockerspawner.DockerSpawner"

network_name = env("DOCKER_NETWORK_NAME", "jupyterhub_network")
c.DockerSpawner.network_name = network_name
c.DockerSpawner.image = env("DOCKER_JUPYTER_IMAGE", "quay.io/jupyter/minimal-notebook:latest")
c.DockerSpawner.pull_policy = env("DOCKER_PULL_POLICY", "ifnotpresent")
c.DockerSpawner.remove = env_bool("DOCKER_SPAWNER_REMOVE", True)
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.start_timeout = env_int("DOCKER_SPAWNER_START_TIMEOUT", 300)
c.DockerSpawner.http_timeout = env_int("DOCKER_SPAWNER_HTTP_TIMEOUT", 120)
c.DockerSpawner.name_template = env(
    "DOCKER_SPAWNER_NAME_TEMPLATE", "jupyter-{username}"
)

notebook_dir = env("DOCKER_NOTEBOOK_DIR", "/home/jovyan/work")
c.DockerSpawner.notebook_dir = notebook_dir

# Host paths for bind mounts (Docker daemon sees these). Compose-adjacent defaults
# are ./data and ./workspaces — set absolute DATA_HOST_PATH / WORKSPACES_HOST_PATH.
data_host_path = env("DATA_HOST_PATH")
workspaces_root = env("WORKSPACES_ROOT", "/workspaces") or "/workspaces"
workspaces_host_path = env("WORKSPACES_HOST_PATH")
# Default under notebook_dir so JupyterLab's file browser shows them immediately.
workspace_mount_base = env(
    "WORKSPACE_MOUNT_BASE",
    f"{notebook_dir.rstrip('/')}/workspaces",
) or f"{notebook_dir.rstrip('/')}/workspaces"
workspace_scope_prefix = env("WORKSPACE_SCOPE_PREFIX", "jupyter:workspace:") or (
    "jupyter:workspace:"
)
workspace_fs_uid = env_int("WORKSPACE_FS_UID", 1000)
workspace_fs_gid = env_int("WORKSPACE_FS_GID", 100)

if data_host_path:
    personal_volume = os.path.join(data_host_path, "users", "{username}")
else:
    personal_volume = "jupyterhub-user-{username}"

c.DockerSpawner.volumes = {personal_volume: notebook_dir}


def _sanitize_workspace_name(name: str) -> str | None:
    name = name.strip()
    if not name or not _WORKSPACE_NAME_RE.fullmatch(name):
        return None
    if name in {".", ".."}:
        return None
    return name


def _workspace_names_from_scopes(scopes: list[str]) -> list[str]:
    names: list[str] = []
    for scope in scopes:
        if not scope.startswith(workspace_scope_prefix):
            continue
        raw = scope[len(workspace_scope_prefix) :]
        safe = _sanitize_workspace_name(raw)
        if safe and safe not in names:
            names.append(safe)
    return names


def _ensure_workspace_dir(name: str) -> str:
    """Create workspace dir inside the Hub mount; return host path for Docker binds."""
    container_path = os.path.join(workspaces_root, name)
    os.makedirs(container_path, exist_ok=True)
    try:
        os.chown(container_path, workspace_fs_uid, workspace_fs_gid)
    except PermissionError:
        log.warning("Could not chown %s to %s:%s", container_path, workspace_fs_uid, workspace_fs_gid)
    if workspaces_host_path:
        return os.path.join(workspaces_host_path, name)
    return container_path


def configure_spawner_volumes(spawner, workspaces: list[str]) -> None:
    """Set personal + shared workspace bind mounts on a Spawner."""
    volumes: dict[str, str] = {personal_volume: notebook_dir}

    if data_host_path:
        user_dir = os.path.join(data_host_path, "users", spawner.user.name)
        container_user_dir = os.path.join("/data", "users", spawner.user.name)
        os.makedirs(container_user_dir, exist_ok=True)
        try:
            os.chown(container_user_dir, workspace_fs_uid, workspace_fs_gid)
        except PermissionError:
            log.warning("Could not chown %s", container_user_dir)
        volumes = {user_dir: notebook_dir}

    mounted: list[str] = []
    for name in workspaces:
        safe = _sanitize_workspace_name(str(name))
        if not safe:
            log.warning("Skipping invalid workspace name %r", name)
            continue
        host_path = _ensure_workspace_dir(safe)
        mount_path = f"{workspace_mount_base.rstrip('/')}/{safe}"
        volumes[host_path] = mount_path
        mounted.append(f"{host_path} -> {mount_path}")

    spawner.volumes = volumes
    log.info(
        "Configured spawn volumes for %s: personal + %s",
        spawner.user.name,
        mounted or "no shared workspaces",
    )


# Ensure shared root exists in the Hub container on startup.
os.makedirs(workspaces_root, exist_ok=True)
if workspaces_host_path and not os.path.isdir(workspaces_root):
    log.warning(
        "WORKSPACES_ROOT=%s is missing inside the Hub container; "
        "check compose bind for WORKSPACES_HOST_PATH=%s",
        workspaces_root,
        workspaces_host_path,
    )


async def pre_spawn_configure_volumes(spawner) -> None:
    auth_state = await spawner.user.get_auth_state() or {}
    workspaces = list(
        getattr(spawner, "workspaces", None)
        or auth_state.get("workspaces")
        or []
    )
    configure_spawner_volumes(spawner, workspaces)


def auth_state_hook(spawner, auth_state) -> None:
    """Stash workspaces on the spawner so pre_spawn_hook always sees them."""
    workspaces = (auth_state or {}).get("workspaces") or []
    spawner.workspaces = workspaces
    log.info("auth_state_hook user=%s workspaces=%s", spawner.user.name, workspaces)


c.Spawner.pre_spawn_hook = pre_spawn_configure_volumes
c.DockerSpawner.pre_spawn_hook = pre_spawn_configure_volumes
c.Spawner.auth_state_hook = auth_state_hook
c.DockerSpawner.auth_state_hook = auth_state_hook

# ---------------------------------------------------------------------------
# Logto workspace scope poller + OIDC
# ---------------------------------------------------------------------------


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> Any:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            if not payload:
                return None
            return json.loads(payload)
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {err_body or exc.reason}") from exc


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception:
        return {}


def _get_m2m_token() -> str | None:
    global _m2m_token, _m2m_token_expires_at

    endpoint = (env("LOGTO_ENDPOINT") or "").rstrip("/")
    app_id = env("LOGTO_M2M_APP_ID")
    app_secret = env("LOGTO_M2M_APP_SECRET")
    management_resource = env("LOGTO_MANAGEMENT_API_RESOURCE")
    if not endpoint or not app_id or not app_secret or not management_resource:
        return None

    now = time.time()
    if _m2m_token and now < (_m2m_token_expires_at - 60):
        return _m2m_token

    # Logto accepts client_id/secret in the body (preferred) and/or Basic auth.
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": app_secret,
            "resource": management_resource,
            "scope": "all",
        }
    ).encode("utf-8")
    data = _http_json(
        "POST",
        f"{endpoint}/oidc/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    token = data.get("access_token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError(f"Logto M2M token response missing access_token: {data!r}")
    expires_in = int(data.get("expires_in") or 3600)
    _m2m_token = token
    _m2m_token_expires_at = now + expires_in
    return token


def _fetch_workspace_scopes_from_logto() -> list[str]:
    endpoint = (env("LOGTO_ENDPOINT") or "").rstrip("/")
    oauth_resource = env("OAUTH_RESOURCE", "https://jupyter.kumpe.app")
    token = _get_m2m_token()
    if not token or not endpoint:
        return []

    resources = _http_json(
        "GET",
        f"{endpoint}/api/resources?includeScopes=true&page_size=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    if not isinstance(resources, list):
        raise RuntimeError(f"Unexpected Logto resources response: {resources!r}")

    matched = None
    for resource in resources:
        if isinstance(resource, dict) and resource.get("indicator") == oauth_resource:
            matched = resource
            break
    if matched is None:
        indicators = [
            r.get("indicator")
            for r in resources
            if isinstance(r, dict) and r.get("indicator")
        ]
        log.warning(
            "No Logto API resource with indicator %s (found: %s)",
            oauth_resource,
            indicators,
        )
        return []

    scopes = matched.get("scopes") or []
    if not scopes:
        resource_id = matched.get("id")
        if resource_id:
            scopes = _http_json(
                "GET",
                f"{endpoint}/api/resources/{resource_id}/scopes?page_size=100",
                headers={"Authorization": f"Bearer {token}"},
            ) or []

    names: list[str] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        name = scope.get("name") or ""
        if name.startswith(workspace_scope_prefix) and name not in names:
            names.append(name)
    return names


def get_cached_workspace_scopes() -> list[str]:
    with _workspace_scopes_lock:
        return list(_workspace_scopes)


def refresh_workspace_scope_cache() -> None:
    try:
        scopes = _fetch_workspace_scopes_from_logto()
    except Exception as exc:
        log.warning("Workspace scope poll failed; keeping previous cache: %s", exc)
        return

    with _workspace_scopes_lock:
        _workspace_scopes[:] = scopes
    log.info("Polled Logto workspace scopes: %s", scopes)


_oauth_scope_list: list[str] = []


def _rebuild_oauth_scope_list() -> None:
    base = list(env_set("OAUTH_SCOPE") or {"openid", "profile", "email"})
    for scope in get_cached_workspace_scopes():
        if scope not in base:
            base.append(scope)
    _oauth_scope_list[:] = base


def _poll_loop() -> None:
    interval = env_int("WORKSPACE_SCOPE_POLL_SECONDS", 300)
    while True:
        refresh_workspace_scope_cache()
        _rebuild_oauth_scope_list()
        time.sleep(max(interval, 30))


def _start_workspace_scope_poller() -> None:
    if not env("LOGTO_M2M_APP_ID") or not env("LOGTO_M2M_APP_SECRET"):
        log.info("Logto M2M not configured; workspace scope polling disabled")
        return
    refresh_workspace_scope_cache()
    _rebuild_oauth_scope_list()
    thread = threading.Thread(target=_poll_loop, name="logto-workspace-scope-poller", daemon=True)
    thread.start()


def _scopes_from_token_info(token_info: dict[str, Any]) -> list[str]:
    scope = token_info.get("scope", "")
    scopes: list[str] = []
    if isinstance(scope, str) and scope.strip():
        if " " in scope:
            scopes = [s for s in scope.split(" ") if s]
        elif "," in scope:
            scopes = [s for s in scope.split(",") if s]
        else:
            scopes = [scope]
    elif isinstance(scope, list):
        scopes = [str(s) for s in scope]

    access_token = token_info.get("access_token")
    if access_token:
        payload = _decode_jwt_payload(access_token)
        claim = payload.get("scope", "")
        if isinstance(claim, str) and claim.strip():
            for item in claim.split(" "):
                if item and item not in scopes:
                    scopes.append(item)
        elif isinstance(claim, list):
            for item in claim:
                text = str(item)
                if text and text not in scopes:
                    scopes.append(text)
    return scopes


# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------
authenticator = (env("AUTHENTICATOR", "dummy") or "dummy").strip().lower()

if authenticator == "dummy":
    c.JupyterHub.authenticator_class = "dummy"
    c.DummyAuthenticator.password = env("DUMMY_PASSWORD", "")

elif authenticator == "github":
    from oauthenticator.github import GitHubOAuthenticator

    c.JupyterHub.authenticator_class = GitHubOAuthenticator
    c.GitHubOAuthenticator.client_id = env("OAUTH_CLIENT_ID")
    c.GitHubOAuthenticator.client_secret = env("OAUTH_CLIENT_SECRET")
    c.GitHubOAuthenticator.oauth_callback_url = env("OAUTH_CALLBACK_URL")
    allowed_orgs = env_set("GITHUB_ALLOWED_ORGANIZATIONS")
    if allowed_orgs:
        c.GitHubOAuthenticator.allowed_organizations = allowed_orgs

elif authenticator == "generic":
    from oauthenticator.generic import GenericOAuthenticator
    from oauthenticator.oauth2 import OAuthLoginHandler

    class LogtoLoginHandler(OAuthLoginHandler):
        """Refresh polled workspace scopes immediately before authorize redirect."""

        def get(self):
            _rebuild_oauth_scope_list()
            self.authenticator.scope = list(_oauth_scope_list)
            self.log.info("OAuth authorize scopes: %s", self.authenticator.scope)
            return super().get()

    class LogtoOAuthenticator(GenericOAuthenticator):
        """Generic OIDC with Logto resource param and workspace scopes from poller."""

        login_handler = LogtoLoginHandler

        async def authenticate(self, handler, data=None):
            result = await super().authenticate(handler, data)
            if not result:
                return result
            if isinstance(result, str):
                return result
            auth_state = result.get("auth_state") or {}
            token_info = auth_state.get("token_response") or {}
            if not token_info and auth_state.get("access_token"):
                token_info = {
                    "access_token": auth_state.get("access_token"),
                    "scope": auth_state.get("scope") or [],
                }
            scopes = _scopes_from_token_info(token_info)
            if not scopes and isinstance(auth_state.get("scope"), list):
                scopes = [str(s) for s in auth_state["scope"]]
            workspaces = _workspace_names_from_scopes(scopes)
            auth_state["workspaces"] = workspaces
            auth_state["workspace_scopes"] = [
                f"{workspace_scope_prefix}{name}" for name in workspaces
            ]
            result["auth_state"] = auth_state
            self.log.info(
                "Logto workspaces for %s: %s",
                result.get("name"),
                workspaces,
            )
            return result

        async def pre_spawn_start(self, user, spawner):
            """Apply shared workspace mounts before the notebook container starts."""
            auth_state = await user.get_auth_state() or {}
            workspaces = list(auth_state.get("workspaces") or [])
            self.log.info(
                "pre_spawn_start user=%s auth_state_workspaces=%s",
                user.name,
                workspaces,
            )
            configure_spawner_volumes(spawner, workspaces)

    c.JupyterHub.authenticator_class = LogtoOAuthenticator
    c.LogtoOAuthenticator.client_id = env("OAUTH_CLIENT_ID")
    c.LogtoOAuthenticator.client_secret = env("OAUTH_CLIENT_SECRET")
    c.LogtoOAuthenticator.oauth_callback_url = env("OAUTH_CALLBACK_URL")
    c.LogtoOAuthenticator.authorize_url = env("OAUTH_AUTHORIZE_URL")
    c.LogtoOAuthenticator.token_url = env("OAUTH_TOKEN_URL")
    c.LogtoOAuthenticator.username_claim = env(
        "OAUTH_USERNAME_CLAIM", "preferred_username"
    )
    userdata_key = env("OAUTH_USERDATA_USERNAME_KEY")
    if userdata_key:
        c.LogtoOAuthenticator.username_key = userdata_key

    _rebuild_oauth_scope_list()
    c.LogtoOAuthenticator.scope = _oauth_scope_list
    c.LogtoOAuthenticator.enable_auth_state = True

    # Logto access tokens for an API resource are JWTs that /oidc/me rejects.
    # Take profile claims from the ID token instead.
    oauth_resource = env("OAUTH_RESOURCE", "https://jupyter.kumpe.app")
    if oauth_resource:
        c.LogtoOAuthenticator.extra_authorize_params = {"resource": oauth_resource}
        c.LogtoOAuthenticator.token_params = {"resource": oauth_resource}
        c.LogtoOAuthenticator.userdata_from_id_token = True
    else:
        c.LogtoOAuthenticator.userdata_url = env("OAUTH_USERDATA_URL")

    if not crypt_key:
        log.warning(
            "JUPYTERHUB_CRYPT_KEY is unset; auth_state (workspaces) will not persist. "
            "Generate with: openssl rand -hex 32"
        )

    _start_workspace_scope_poller()

else:
    print(f"Unsupported AUTHENTICATOR={authenticator!r}; use dummy|github|generic", file=sys.stderr)
    sys.exit(1)
