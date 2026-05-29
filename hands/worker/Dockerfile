FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /opt/friday

ARG GMAIL_MCP_REF=c9a31741c7e879575c9dfc90192a7a430efef812
ARG RESTAURANT_CLI_REF=78547125e8bb0faf79dd70c87e608f93b4004e68

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHROME_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome \
    STAGEHAND_LOCAL_CHROME_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome

COPY hands/worker/patch_restaurant_cli_browser.py /tmp/patch_restaurant_cli_browser.py
COPY hands/worker/patch_strider_opentable_browser.py /tmp/patch_strider_opentable_browser.py

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
        playwright@1.49.1 \
        @modelcontextprotocol/server-filesystem \
        firecrawl-mcp \
        @cablate/mcp-google-map \
        @openapi-mcp/server \
        @striderlabs/mcp-opentable \
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
    && PLAYWRIGHT_BROWSERS_PATH=/ms-playwright node /usr/lib/node_modules/playwright/cli.js install firefox \
    && python /tmp/patch_restaurant_cli_browser.py /opt/friday/tools/restaurant-cli/dist/src/providers/opentable/browser.js \
    && python /tmp/patch_strider_opentable_browser.py /usr/lib/node_modules/@striderlabs/mcp-opentable/dist/browser.js \
    && if [ -d /ms-playwright/firefox-1522 ] && [ ! -e /ms-playwright/firefox-1466 ]; then ln -s /ms-playwright/firefox-1522 /ms-playwright/firefox-1466; fi \
    && mkdir -p /opt/google/chrome \
    && ln -sf /ms-playwright/chromium-1148/chrome-linux/chrome /opt/google/chrome/chrome \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY hands/worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && rm -rf /root/.cache /tmp/*

COPY agent/app ./app
COPY hands/worker/runner.py ./runner.py
COPY hands/worker/temporal_activity_worker.py ./temporal_activity_worker.py

CMD ["python", "/opt/friday/runner.py"]
