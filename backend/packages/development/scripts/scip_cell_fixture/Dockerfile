# Test-only Cell image. Network access is used at build time, never at test runtime.
FROM node:22.23.5-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS node
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY scip.tgz /tmp/scip.tgz
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && \
    python -m pip install --no-cache-dir 'pytest==9.2.0' 'pydantic==2.14.3' 'jsonschema==4.26.0' 'protobuf==6.33.6' && \
    mkdir -p /usr/local/lib/scip-python /usr/local/share && \
    tar -xzf /tmp/scip.tgz -C /usr/local/lib/scip-python --strip-components=1 && \
    rm /tmp/scip.tgz && printf '[]\n' >/usr/local/share/aiagent-scip-environment.json
