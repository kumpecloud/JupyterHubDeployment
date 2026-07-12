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

# Private package? authenticate once:
# echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

docker compose pull
docker compose up -d
```

Hub listens on `http://localhost:8000` (or `JUPYTERHUB_PORT`).

With `AUTHENTICATOR=dummy` (default), any username works unless `DUMMY_PASSWORD` is set.

## Configuration

All runtime settings come from `.env` — see `.env.example`. The baked-in `jupyterhub_config.py` reads those variables; you do not need to mount a config file.

| Variable | Purpose |
| --- | --- |
| `JUPYTERHUB_IMAGE` | GHCR image to run |
| `DOCKER_JUPYTER_IMAGE` | Per-user notebook image |
| `DOCKER_NETWORK_NAME` | Shared Docker network |
| `AUTHENTICATOR` | `dummy`, `github`, or `generic` (OIDC) |
| `JUPYTERHUB_ADMIN_USERS` | Comma-separated admins |

## Local image build (optional)

```bash
docker build -t jupyterhubdeployment:local .
JUPYTERHUB_IMAGE=jupyterhubdeployment:local docker compose up -d
```
