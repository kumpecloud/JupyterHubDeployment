"""JupyterHub config driven entirely by environment variables.

Bake this file into the image; override behavior at deploy time with .env.
"""

from __future__ import annotations

import os
import sys

c = get_config()  # noqa: F821


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


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------
c.JupyterHub.bind_url = env("JUPYTERHUB_BIND_URL", "http://:8000")
# Bind on all interfaces; other containers reach the Hub via hub_connect_ip.
c.JupyterHub.hub_ip = env("JUPYTERHUB_HUB_IP", "0.0.0.0")
c.JupyterHub.hub_connect_ip = env("JUPYTERHUB_HUB_CONNECT_IP", "jupyterhub")
c.JupyterHub.db_url = env("JUPYTERHUB_DB_URL", "sqlite:////data/jupyterhub.sqlite")
c.JupyterHub.cookie_secret_file = env(
    "JUPYTERHUB_COOKIE_SECRET_FILE", "/data/jupyterhub_cookie_secret"
)

admin_users = env_set("JUPYTERHUB_ADMIN_USERS")
if admin_users:
    c.Authenticator.admin_users = admin_users

allowed_users = env_set("JUPYTERHUB_ALLOWED_USERS")
if allowed_users:
    c.Authenticator.allowed_users = allowed_users

# ---------------------------------------------------------------------------
# Spawner (Docker)
# ---------------------------------------------------------------------------
c.JupyterHub.spawner_class = "dockerspawner.DockerSpawner"

network_name = env("DOCKER_NETWORK_NAME", "jupyterhub_network")
c.DockerSpawner.network_name = network_name
c.DockerSpawner.image = env("DOCKER_JUPYTER_IMAGE", "quay.io/jupyter/minimal-notebook:latest")
c.DockerSpawner.remove = env_bool("DOCKER_SPAWNER_REMOVE", True)
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.start_timeout = int(env("DOCKER_SPAWNER_START_TIMEOUT", "180") or "180")
c.DockerSpawner.http_timeout = int(env("DOCKER_SPAWNER_HTTP_TIMEOUT", "120") or "120")
c.DockerSpawner.name_template = env(
    "DOCKER_SPAWNER_NAME_TEMPLATE", "jupyter-{username}"
)

notebook_dir = env("DOCKER_NOTEBOOK_DIR", "/home/jovyan/work")
c.DockerSpawner.notebook_dir = notebook_dir
c.DockerSpawner.volumes = {
    "jupyterhub-user-{username}": notebook_dir,
}

# ---------------------------------------------------------------------------
# Authenticator
# ---------------------------------------------------------------------------
# AUTHENTICATOR: dummy | github | generic
authenticator = (env("AUTHENTICATOR", "dummy") or "dummy").strip().lower()

if authenticator == "dummy":
    c.JupyterHub.authenticator_class = "dummy"
    # Empty password allows any username when DummyAuthenticator is used.
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

    c.JupyterHub.authenticator_class = GenericOAuthenticator
    c.GenericOAuthenticator.client_id = env("OAUTH_CLIENT_ID")
    c.GenericOAuthenticator.client_secret = env("OAUTH_CLIENT_SECRET")
    c.GenericOAuthenticator.oauth_callback_url = env("OAUTH_CALLBACK_URL")
    c.GenericOAuthenticator.authorize_url = env("OAUTH_AUTHORIZE_URL")
    c.GenericOAuthenticator.token_url = env("OAUTH_TOKEN_URL")
    c.GenericOAuthenticator.userdata_url = env("OAUTH_USERDATA_URL")
    c.GenericOAuthenticator.username_claim = env("OAUTH_USERNAME_CLAIM", "preferred_username")
    c.GenericOAuthenticator.scope = list(env_set("OAUTH_SCOPE") or {"openid", "profile", "email"})
    userdata_key = env("OAUTH_USERDATA_USERNAME_KEY")
    if userdata_key:
        c.GenericOAuthenticator.username_key = userdata_key

else:
    print(f"Unsupported AUTHENTICATOR={authenticator!r}; use dummy|github|generic", file=sys.stderr)
    sys.exit(1)
