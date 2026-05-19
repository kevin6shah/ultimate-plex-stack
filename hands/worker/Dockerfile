FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /opt/friday

ARG RESY_MCP_REF=083294fa429616d1a6eccc7e3d524438d12aede5
ARG GMAIL_MCP_REF=c9a31741c7e879575c9dfc90192a7a430efef812
ARG RESTAURANT_CLI_REF=78547125e8bb0faf79dd70c87e608f93b4004e68

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install -g \
        playwright \
        @modelcontextprotocol/server-filesystem \
        firecrawl-mcp \
        @cablate/mcp-google-map \
        @openapi-mcp/server \
        @striderlabs/mcp-opentable \
    && git clone https://github.com/Jpc54066/resy-mcp.git /opt/friday/mcp/resy-mcp \
    && cd /opt/friday/mcp/resy-mcp \
    && git checkout "${RESY_MCP_REF}" \
    && npm install \
    && npm run build \
    && git clone https://github.com/Sallytion/Gmail-MCP.git /opt/friday/mcp/gmail-mcp \
    && cd /opt/friday/mcp/gmail-mcp \
    && git checkout "${GMAIL_MCP_REF}" \
    && npm install \
    && npm run build \
    && git clone https://github.com/omarshahine/restaurant-cli.git /opt/friday/tools/restaurant-cli \
    && cd /opt/friday/tools/restaurant-cli \
    && git checkout "${RESTAURANT_CLI_REF}" \
    && npm install \
    && npm link \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY hands/worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && rm -rf /root/.cache /tmp/*

COPY agent/app ./app
COPY hands/worker/runner.py ./runner.py

CMD ["python", "/opt/friday/runner.py"]
