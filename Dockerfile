FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates ripgrep ffmpeg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

RUN git clone --recurse-submodules https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent

WORKDIR /opt/hermes-agent
RUN uv venv venv --python 3.11 \
    && VIRTUAL_ENV=/opt/hermes-agent/venv uv pip install -e ".[all,messaging]"

ENV PATH="/opt/hermes-agent/venv/bin:$PATH"

RUN mkdir -p /root/.hermes/{cron,sessions,logs,memories,skills,pairing,hooks,image_cache,audio_cache} \
    && cp cli-config.yaml.example /root/.hermes/config.yaml \
    && touch /root/.hermes/.env

COPY auth_proxy.py /auth_proxy.py
COPY entrypoint.sh /entrypoint.sh
COPY patches/apply_patches.py /opt/hermes-agent/patches/apply_patches.py
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Apply patches: show all providers (not just authenticated) in model picker
# This lets users see and activate Ollama Cloud and other providers
# by entering API keys directly from the web UI.
RUN python3 /opt/hermes-agent/patches/apply_patches.py

# Build the web frontend with our patched TypeScript
RUN cd /opt/hermes-agent/web && npm install && npm run build

ENTRYPOINT ["/entrypoint.sh"]