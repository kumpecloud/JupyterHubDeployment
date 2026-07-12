FROM jupyterhub/jupyterhub:latest

RUN pip install --no-cache-dir \
    dockerspawner \
    oauthenticator

COPY jupyterhub_config.py /srv/jupyterhub/jupyterhub_config.py

WORKDIR /srv/jupyterhub

CMD ["jupyterhub", "-f", "/srv/jupyterhub/jupyterhub_config.py"]
