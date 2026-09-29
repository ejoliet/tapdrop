# tapdrop image: one process, no database server, data mounted in at /data.
#
# The image binds 0.0.0.0 because a container port is only reachable that way;
# the localhost-only default in the CLI still applies everywhere else. Running
# the container is a deliberate public-exposure decision, exactly like --share.
FROM python:3.12-slim AS build

# uv builds the wheel; the runtime image gets no build tooling at all.
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /usr/local/bin/uv

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist


FROM python:3.12-slim

LABEL org.opencontainers.image.title="tapdrop" \
      org.opencontainers.image.description="Temporary, standards-compliant IVOA TAP service over files in place" \
      org.opencontainers.image.source="https://github.com/ejoliet/tapdrop" \
      org.opencontainers.image.licenses="MIT"

COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# Non-root: the service only ever reads the mounted data.
RUN useradd --create-home --uid 10001 tapdrop
USER tapdrop
WORKDIR /home/tapdrop

ENV TAPDROP_HOST=0.0.0.0 \
    TAPDROP_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# `docker run ... tapdrop:local serve /data` reads naturally, so the command line
# is the CLI's own; the entrypoint is a two-line shim that only maps a host's
# $PORT onto TAPDROP_PORT before exec'ing the CLI.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
