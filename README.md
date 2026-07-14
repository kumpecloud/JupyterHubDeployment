# JupyterHubDeployment

Custom JupyterHub image (DockerSpawner + OAuthenticator) published to GHCR. Deploy with only `docker-compose.yml` and a `.env` file.

## Image

Built and pushed by [`.github/workflows/publish-ghcr.yml`](.github/workflows/publish-ghcr.yml):

```text
ghcr.io/kumpecloud/jupyterhubdeployment:latest
```

Tags also include `sha-<commit>` and semver tags from `v*` git tags.

## Deploy

```bash
cp .env.example .env
# edit .env as needed

# Absolute paths for DockerSpawner binds (same dirs as compose mounts):
export DATA_HOST_PATH="$(pwd)/data"
export WORKSPACES_HOST_PATH="$(pwd)/workspaces"
# put those into .env as well

mkdir -p data workspaces
openssl rand -hex 32  # set as JUPYTERHUB_CRYPT_KEY

# Private package? authenticate once:
# echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

docker compose pull
docker compose up -d
```

Hub listens on `http://localhost:8000` (or `JUPYTERHUB_PORT`).

Persistent data lives next to compose (not named volumes):

| Host path | Purpose |
| --- | --- |
| `./data` | Hub DB, cookie secret |
| `./data/users/<username>` | Personal notebook home |
| `./workspaces/<name>` | Shared workspaces from Logto |

## Logto shared workspaces

1. API resource indicator = `OAUTH_RESOURCE` (e.g. `https://jupyter.kumpe.app`)
2. Permissions named `jupyter:workspace:<name>` (e.g. `jupyter:workspace:public`)
3. Assign via roles to users
4. Create an M2M app with Management API access; set `LOGTO_M2M_*` and `LOGTO_MANAGEMENT_API_RESOURCE=https://default.kumpe.app/api` in `.env`
5. Hub polls the API resource scopes and requests them on every login
After adding a **new** workspace permission: wait for the poll, then logout/login once (or wait until users next spawn — spawn refreshes grants from Logto).

Revoking access: spawn re-checks Logto roles via Management API, and Hub logout clears grants and stops the user server so live mounts do not linger.

Granted workspaces appear in the JupyterLab **File Browser** under `workspaces/<name>` (mounted at `/home/jovyan/work/workspaces/<name>`).

Note: the JupyterLab URL `/lab/workspaces/auto-P` is JupyterLab’s own UI layout “workspace,” not a shared data folder. Use the file browser (left sidebar) to open shared folders.

## Idle servers

Running notebooks are stopped after `IDLE_CULL_TIMEOUT_SECONDS` of inactivity (default 3600). Containers are removed (`DOCKER_SPAWNER_REMOVE`).

Optional: set `INACTIVE_USER_CULL_DAYS=30` to delete Hub user records that have been inactive that long. Users can log in again; `./data/users/<name>` and shared `./workspaces` are not auto-deleted.

## Configuration

All runtime settings come from `.env` — see `.env.example`. The baked-in `jupyterhub_config.py` reads those variables; you do not need to mount a config file.

| Variable | Purpose |
| --- | --- |
| `JUPYTERHUB_IMAGE` | GHCR image to run |
| `DOCKER_JUPYTER_IMAGE` | Per-user notebook image |
| `DOCKER_NETWORK_NAME` | Shared Docker network |
| `AUTHENTICATOR` | `dummy`, `github`, or `generic` (OIDC) |
| `JUPYTERHUB_ADMIN_USERS` | Comma-separated admins |
| `DATA_HOST_PATH` / `WORKSPACES_HOST_PATH` | Absolute host paths for binds |
| `JUPYTERHUB_CRYPT_KEY` | Encrypts auth_state (required for workspaces) |
| `IDLE_CULL_TIMEOUT_SECONDS` | Stop idle running servers |
| `INACTIVE_USER_CULL_DAYS` | Delete inactive Hub users (0 = off) |

## Local image build (optional)

```bash
docker build -t jupyterhubdeployment:local .
JUPYTERHUB_IMAGE=jupyterhubdeployment:local docker compose up -d
```
