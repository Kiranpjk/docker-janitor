FROM docker:29-cli AS dockercli

FROM python:3.15-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY docker_janitor ./docker_janitor
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.15-slim
# Only the static docker CLI binary is needed; it talks to the host daemon via the mounted socket.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=build /install /usr/local
ENTRYPOINT ["docker-janitor"]
