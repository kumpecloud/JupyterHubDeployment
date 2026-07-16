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
_cookie_secret_file = (
    env("JUPYTERHUB_COOKIE_SECRET_FILE", "/data/jupyterhub_cookie_secret")
    or "/data/jupyterhub_cookie_secret"
)
c.JupyterHub.cookie_secret_file = _cookie_secret_file


def _ensure_private_cookie_secret_file(path: str) -> None:
    """JupyterHub refuses cookie secrets that are group/world accessible."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("Could not chmod cookie_secret_file %s to 0600: %s", path, exc)
        return
    # Pre-create with safe mode so a loose container umask cannot leave it world-readable.
    secret = os.urandom(32).hex().encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            log.warning("Could not chmod cookie_secret_file %s to 0600: %s", path, exc)
        return
    except OSError as exc:
        log.warning("Could not create cookie_secret_file %s: %s", path, exc)
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    log.info("Created cookie_secret_file %s with mode 0600", path)


_ensure_private_cookie_secret_file(_cookie_secret_file)

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

# Stop user servers on Hub logout so revoked mounts cannot linger in live containers.
c.JupyterHub.shutdown_on_logout = env_bool("SHUTDOWN_ON_LOGOUT", True)

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
c.DockerSpawner.image = env(
    "DOCKER_JUPYTER_IMAGE",
    "ghcr.io/kumpecloud/jupyterhubdeployment-notebook:latest",
)
c.DockerSpawner.environment = {
    'MYSQL_HOST': env("DOCKER_SPAWNER_MYSQL_HOST", ""),
    'MYSQL_USER': env("DOCKER_SPAWNER_MYSQL_USER", ""),
    'MYSQL_PASSWORD': env("DOCKER_SPAWNER_MYSQL_PASSWORD", ""),
    'MYSQL_DATABASE': env("DOCKER_SPAWNER_MYSQL_DATABASE", "")
}
c.DockerSpawner.pull_policy = env("DOCKER_PULL_POLICY", "always")
c.DockerSpawner.remove = env_bool("DOCKER_SPAWNER_REMOVE", True)
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.start_timeout = env_int("DOCKER_SPAWNER_START_TIMEOUT", 300)
c.DockerSpawner.http_timeout = env_int("DOCKER_SPAWNER_HTTP_TIMEOUT", 120)
c.DockerSpawner.name_template = env(
    "DOCKER_SPAWNER_NAME_TEMPLATE", "jupyter-{username}"
)

notebook_dir = env("DOCKER_NOTEBOOK_DIR", "/home/jovyan/work")
c.DockerSpawner.notebook_dir = notebook_dir

# Host paths for bind mounts (Docker daemon sees these). Relative values like
# ./data are rejected by Docker — resolve against HOST_PROJECT_DIR when needed.
host_project_dir = env("HOST_PROJECT_DIR")


def _absolute_host_path(path: str | None, *, label: str) -> str | None:
    """Return an absolute host path suitable for Docker bind mounts, or None."""
    if not path:
        return None
    path = path.strip()
    if not path:
        return None
    if os.path.isabs(path):
        return os.path.normpath(path)
    if host_project_dir and os.path.isabs(host_project_dir):
        resolved = os.path.normpath(os.path.join(host_project_dir, path))
        log.warning(
            "%s=%r is not absolute; resolved to %s via HOST_PROJECT_DIR",
            label,
            path,
            resolved,
        )
        return resolved
    log.error(
        "%s=%r is not absolute and HOST_PROJECT_DIR is unset/relative. "
        "Docker bind mounts require absolute host paths "
        "(e.g. /home/.../data). Falling back to named volumes.",
        label,
        path,
    )
    return None


data_host_path = _absolute_host_path(env("DATA_HOST_PATH"), label="DATA_HOST_PATH")
workspaces_root = env("WORKSPACES_ROOT", "/workspaces") or "/workspaces"
workspaces_host_path = _absolute_host_path(
    env("WORKSPACES_HOST_PATH"), label="WORKSPACES_HOST_PATH"
)
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
log.info(
    "Spawn storage: data_host=%s workspaces_host=%s personal=%s",
    data_host_path,
    workspaces_host_path,
    personal_volume,
)


def _sanitize_workspace_name(name: str) -> str | None:
    name = name.strip()
    if not name or not _WORKSPACE_NAME_RE.fullmatch(name):
        return None
    if name in {".", ".."}:
        return None
    return name


def _normalize_access_mode(mode: str | None) -> str:
    """Return Docker bind mode: 'ro' or 'rw'."""
    value = (mode or "").strip().lower()
    if value in {"ro", "read", "readonly", "read-only"}:
        return "ro"
    return "rw"


def _normalize_workspace_grants(workspaces: Any) -> dict[str, str]:
    """Normalize grants to {workspace_name: 'ro'|'rw'}.

    Accepts:
    - dict mapping name -> mode
    - list of workspace names (legacy; treated as rw)
    """
    grants: dict[str, str] = {}
    if isinstance(workspaces, dict):
        for name, mode in workspaces.items():
            safe = _sanitize_workspace_name(str(name))
            if not safe:
                continue
            normalized = _normalize_access_mode(str(mode) if mode is not None else None)
            if safe not in grants or normalized == "rw":
                grants[safe] = normalized
        return grants
    if isinstance(workspaces, list):
        for item in workspaces:
            if isinstance(item, dict):
                raw_name = item.get("name") or item.get("workspace") or ""
                safe = _sanitize_workspace_name(str(raw_name))
                if not safe:
                    continue
                normalized = _normalize_access_mode(
                    str(item.get("mode") or item.get("access") or "rw")
                )
            else:
                safe = _sanitize_workspace_name(str(item))
                if not safe:
                    continue
                normalized = "rw"
            if safe not in grants or normalized == "rw":
                grants[safe] = normalized
    return grants


def _workspace_grants_from_scopes(scopes: list[str]) -> dict[str, str]:
    """Parse Logto scopes into workspace grants.

    Supported permission names:
      jupyter:workspace:{name}:read   -> read-only mount
      jupyter:workspace:{name}:write  -> read-write mount

    Legacy jupyter:workspace:{name} (no suffix) is treated as write.
    If both read and write are present for the same workspace, write wins.
    """
    grants: dict[str, str] = {}
    for scope in scopes:
        if not scope.startswith(workspace_scope_prefix):
            continue
        raw = scope[len(workspace_scope_prefix) :]
        mode = "rw"
        if raw.endswith(":read"):
            mode = "ro"
            raw = raw[: -len(":read")]
        elif raw.endswith(":write"):
            mode = "rw"
            raw = raw[: -len(":write")]
        safe = _sanitize_workspace_name(raw)
        if not safe:
            continue
        if safe not in grants or mode == "rw":
            grants[safe] = mode
    return grants


def _workspace_names_from_scopes(scopes: list[str]) -> list[str]:
    return list(_workspace_grants_from_scopes(scopes).keys())


def _workspace_scope_strings(grants: dict[str, str]) -> list[str]:
    scopes: list[str] = []
    for name, mode in grants.items():
        suffix = "write" if mode == "rw" else "read"
        scopes.append(f"{workspace_scope_prefix}{name}:{suffix}")
    return scopes


def _workspace_grants_path(username: str) -> str:
    return os.path.join("/data", "workspace-grants", f"{username}.json")


def save_workspace_grants(
    username: str,
    workspaces: dict[str, str] | list[str],
    *,
    logto_user_id: str | None = None,
) -> None:
    """Persist grants on disk so spawn does not depend on encrypted auth_state."""
    path = _workspace_grants_path(username)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    grants = _normalize_workspace_grants(workspaces)
    payload: dict[str, Any] = {"workspaces": grants}
    if logto_user_id:
        payload["logto_user_id"] = logto_user_id
    else:
        # Keep prior Logto user id if we are only refreshing workspace list.
        try:
            with open(path, encoding="utf-8") as handle:
                existing = json.load(handle)
            if existing.get("logto_user_id"):
                payload["logto_user_id"] = existing["logto_user_id"]
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    for name in grants:
        _ensure_workspace_dir(name)
    log.info("Saved workspace grants for %s -> %s (%s)", username, path, grants)


def load_workspace_grants(username: str) -> dict[str, str]:
    path = _workspace_grants_path(username)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return _normalize_workspace_grants(data.get("workspaces") or {})
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Failed reading workspace grants for %s: %s", username, exc)
        return {}


def load_workspace_grant_record(username: str) -> dict[str, Any]:
    path = _workspace_grants_path(username)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("Failed reading workspace grant record for %s: %s", username, exc)
        return {}


def clear_workspace_grants(username: str) -> None:
    path = _workspace_grants_path(username)
    try:
        os.remove(path)
        log.info("Cleared workspace grants for %s", username)
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Failed clearing workspace grants for %s: %s", username, exc)


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


def configure_spawner_volumes(
    spawner, workspaces: dict[str, str] | list[str]
) -> None:
    """Set personal + shared workspace bind mounts on a Spawner."""
    grants = _normalize_workspace_grants(workspaces)
    volumes: dict[str, Any] = {
        personal_volume: {"bind": notebook_dir, "mode": "rw"},
    }

    if data_host_path:
        user_dir = os.path.join(data_host_path, "users", spawner.user.name)
        container_user_dir = os.path.join("/data", "users", spawner.user.name)
        os.makedirs(container_user_dir, exist_ok=True)
        try:
            os.chown(container_user_dir, workspace_fs_uid, workspace_fs_gid)
        except PermissionError:
            log.warning("Could not chown %s", container_user_dir)
        volumes = {
            user_dir: {"bind": notebook_dir, "mode": "rw"},
        }

    mounted: list[str] = []
    for name, mode in grants.items():
        host_path = _ensure_workspace_dir(name)
        mount_path = f"{workspace_mount_base.rstrip('/')}/{name}"
        volumes[host_path] = {"bind": mount_path, "mode": mode}
        mounted.append(f"{host_path} -> {mount_path} ({mode})")

    spawner.volumes = volumes
    log.info(
        "Configured spawn volumes for %s (workspaces_root=%s exists=%s): %s",
        spawner.user.name,
        workspaces_root,
        os.path.isdir(workspaces_root),
        mounted or "no shared workspaces",
    )


# Ensure shared root exists in the Hub container on startup.
os.makedirs(workspaces_root, exist_ok=True)
log.info(
    "Workspace storage: root=%s host=%s exists=%s",
    workspaces_root,
    workspaces_host_path,
    os.path.isdir(workspaces_root),
)


def _grant_file_exists(username: str) -> bool:
    return os.path.exists(_workspace_grants_path(username))


async def pre_spawn_configure_volumes(spawner) -> None:
    # Volumes are normally set in Authenticator.pre_spawn_start. This runs after
    # auth_state_hook and must not reintroduce revoked mounts from stale auth_state.
    workspaces = _normalize_workspace_grants(
        getattr(spawner, "workspaces", None) or {}
    )
    if not workspaces and _grant_file_exists(spawner.user.name):
        workspaces = load_workspace_grants(spawner.user.name)
    configure_spawner_volumes(spawner, workspaces)


def auth_state_hook(spawner, auth_state) -> None:
    """Apply workspaces after Authenticator.pre_spawn_start (spawn order).

    Prefer the grants file written by the live Logto refresh — encrypted
    auth_state can still list revoked workspaces until the next login.
    """
    username = spawner.user.name
    if _grant_file_exists(username):
        workspaces = load_workspace_grants(username)
    else:
        workspaces = _normalize_workspace_grants(
            (auth_state or {}).get("workspaces") or {}
        )
    spawner.workspaces = workspaces
    configure_spawner_volumes(spawner, workspaces)
    log.info("auth_state_hook user=%s workspaces=%s", username, workspaces)


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


def fetch_user_workspaces_from_logto(logto_user_id: str) -> dict[str, str] | None:
    """Return current workspace grants for a Logto user via Management API.

    Returns None on failure so callers can keep the prior grant file.
    An empty dict means the user currently has no workspace grants.
    """
    if not logto_user_id:
        return None
    endpoint = (env("LOGTO_ENDPOINT") or "").rstrip("/")
    oauth_resource = env("OAUTH_RESOURCE", "https://jupyter.kumpe.app")
    try:
        token = _get_m2m_token()
        if not token or not endpoint:
            return None
        roles = _http_json(
            "GET",
            f"{endpoint}/api/users/{urllib.parse.quote(logto_user_id)}/roles?page_size=100",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not isinstance(roles, list):
            raise RuntimeError(f"Unexpected user roles response: {roles!r}")

        scope_names: list[str] = []
        for role in roles:
            if not isinstance(role, dict):
                continue
            role_id = role.get("id")
            if not role_id:
                continue
            role_scopes = _http_json(
                "GET",
                f"{endpoint}/api/roles/{urllib.parse.quote(str(role_id))}/scopes?page_size=100",
                headers={"Authorization": f"Bearer {token}"},
            )
            if not isinstance(role_scopes, list):
                continue
            for scope in role_scopes:
                if not isinstance(scope, dict):
                    continue
                resource = scope.get("resource") or {}
                if isinstance(resource, dict):
                    indicator = resource.get("indicator")
                elif isinstance(resource, str):
                    indicator = resource
                else:
                    indicator = scope.get("resourceId") or scope.get("indicator")
                if oauth_resource and indicator and indicator != oauth_resource:
                    continue
                name = scope.get("name") or ""
                if name and name not in scope_names:
                    scope_names.append(name)
        workspaces = _workspace_grants_from_scopes(scope_names)
        log.info(
            "Logto Management API workspaces for %s: %s (raw scopes=%s roles=%s)",
            logto_user_id,
            workspaces,
            scope_names,
            [r.get("name") for r in roles if isinstance(r, dict)],
        )
        return workspaces
    except Exception as exc:
        log.warning(
            "Failed refreshing Logto workspaces for user id %s: %s",
            logto_user_id,
            exc,
        )
        return None


def resolve_logto_user_id(username: str, auth_state: dict[str, Any] | None = None) -> str | None:
    """Resolve Logto user id from auth_state, grants file, or Management API search."""
    auth_state = auth_state or {}
    for key in ("logto_user_id",):
        value = auth_state.get(key)
        if value:
            return str(value)

    oauth_user = auth_state.get("oauth_user") or {}
    for key in ("sub", "id", "userId"):
        value = oauth_user.get(key)
        if value:
            return str(value)

    id_token = auth_state.get("id_token")
    if id_token:
        sub = _decode_jwt_payload(id_token).get("sub")
        if sub:
            return str(sub)

    record = load_workspace_grant_record(username)
    if record.get("logto_user_id"):
        return str(record["logto_user_id"])

    endpoint = (env("LOGTO_ENDPOINT") or "").rstrip("/")
    try:
        token = _get_m2m_token()
        if not token or not endpoint:
            return None
        # Search users by username (Logto Management API search).
        query = urllib.parse.urlencode(
            {
                "page_size": 20,
                "search_params[mode]": "exact",
                "search_params[fields][]": "username",
                "search": username,
            },
            doseq=True,
        )
        users = _http_json(
            "GET",
            f"{endpoint}/api/users?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if not isinstance(users, list):
            raise RuntimeError(f"Unexpected users search response: {users!r}")
        for user in users:
            if not isinstance(user, dict):
                continue
            if user.get("username") == username or user.get("primaryEmail") == username:
                user_id = user.get("id")
                if user_id:
                    log.info("Resolved Logto user id for %s -> %s", username, user_id)
                    return str(user_id)
        # Fallback: first exact username match after loose search
        for user in users:
            if isinstance(user, dict) and user.get("username") == username and user.get("id"):
                return str(user["id"])
    except Exception as exc:
        log.warning("Failed resolving Logto user id for %s: %s", username, exc)
    return None


def resolve_workspaces_for_spawn(
    username: str,
    auth_state: dict[str, Any] | None = None,
) -> tuple[dict[str, str], bool]:
    """Current workspace grants for spawn, preferring a live Logto Management API refresh.

    Returns (workspaces, refreshed). refreshed=True means Management API answered
    (including an empty dict after revoke). Disk/auth_state are only used on API failure.
    """
    auth_state = auth_state or {}
    logto_user_id = resolve_logto_user_id(username, auth_state)
    refreshed: dict[str, str] | None = None
    if logto_user_id:
        refreshed = fetch_user_workspaces_from_logto(str(logto_user_id))
    if refreshed is not None:
        save_workspace_grants(
            username,
            refreshed,
            logto_user_id=str(logto_user_id) if logto_user_id else None,
        )
        return refreshed, True

    # Prefer the grants file when present (including {}) over stale auth_state.
    if _grant_file_exists(username):
        workspaces = load_workspace_grants(username)
    else:
        workspaces = _normalize_workspace_grants(auth_state.get("workspaces") or {})
    log.warning(
        "Spawn for %s could not refresh from Logto (logto_user_id=%s); "
        "using cached grants=%s",
        username,
        logto_user_id,
        workspaces,
    )
    return workspaces, False


def refresh_workspace_scope_cache() -> None:
    try:
        scopes = _fetch_workspace_scopes_from_logto()
    except Exception as exc:
        log.warning("Workspace scope poll failed; keeping previous cache: %s", exc)
        return

    with _workspace_scopes_lock:
        _workspace_scopes[:] = scopes

    # Create host folders as soon as Logto lists the permissions, so they exist
    # before any user with access logs in.
    created: list[str] = []
    for name in _workspace_names_from_scopes(scopes):
        host_path = _ensure_workspace_dir(name)
        created.append(host_path)
    log.info("Polled Logto workspace scopes: %s (ensured dirs: %s)", scopes, created)


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
    from jupyterhub.handlers.login import LogoutHandler

    class WorkspaceAwareLogoutHandler(LogoutHandler):
        async def default_handle_logout(self):
            user = self.current_user
            if user is not None:
                clear_workspace_grants(user.name)
                self.log.info("Cleared workspace grants on logout for %s", user.name)
            await super().default_handle_logout()

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
        logout_handler = WorkspaceAwareLogoutHandler

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
            # Prefer Logto subject id for later Management API refresh/revoke checks.
            oauth_user = auth_state.get("oauth_user") or {}
            logto_user_id = (
                oauth_user.get("sub")
                or oauth_user.get("id")
                or oauth_user.get("userId")
            )
            id_token = auth_state.get("id_token")
            if not logto_user_id and id_token:
                logto_user_id = _decode_jwt_payload(id_token).get("sub")
            if logto_user_id:
                auth_state["logto_user_id"] = str(logto_user_id)

            # Token scopes can lag role changes; trust Management API when available.
            workspaces = _workspace_grants_from_scopes(scopes)
            if logto_user_id:
                refreshed = fetch_user_workspaces_from_logto(str(logto_user_id))
                if refreshed is not None:
                    workspaces = refreshed
            auth_state["workspaces"] = workspaces
            auth_state["workspace_scopes"] = _workspace_scope_strings(workspaces)
            result["auth_state"] = auth_state
            username = result.get("name") or ""
            if username:
                save_workspace_grants(
                    username,
                    workspaces,
                    logto_user_id=str(logto_user_id) if logto_user_id else None,
                )
            self.log.info(
                "Logto workspaces for %s: %s (logto_user_id=%s)",
                username,
                workspaces,
                logto_user_id,
            )
            return result

        async def pre_spawn_start(self, user, spawner):
            """Apply shared workspace mounts before the notebook container starts."""
            auth_state = await user.get_auth_state() or {}
            workspaces, did_refresh = resolve_workspaces_for_spawn(
                user.name, auth_state
            )
            # Keep encrypted auth_state in sync so later hooks cannot remount revoked dirs.
            auth_state["workspaces"] = workspaces
            auth_state["workspace_scopes"] = _workspace_scope_strings(workspaces)
            logto_user_id = resolve_logto_user_id(user.name, auth_state)
            if logto_user_id:
                auth_state["logto_user_id"] = str(logto_user_id)
            await user.save_auth_state(auth_state)
            spawner.workspaces = workspaces
            self.log.info(
                "pre_spawn_start user=%s workspaces=%s (refreshed=%s)",
                user.name,
                workspaces,
                did_refresh,
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
