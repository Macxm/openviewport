# Test runner: unit tests and the browser end-to-end check, both in a container.
# The Playwright image ships Chromium, Firefox and WebKit plus their system libraries
# for amd64 and arm64. Only Firefox and WebKit decode H.264/H.265, so the e2e check
# uses Firefox; bundled Chromium would need MJPEG.
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /repo

# Dependencies only. Tests run from the bind-mounted source, so editing a test
# or a module needs no rebuild.
COPY agent /tmp/build/agent
COPY mock-nvr/requirements.txt /tmp/build/mock-requirements.txt
RUN pip install --no-cache-dir "/tmp/build/agent[dev]" -r /tmp/build/mock-requirements.txt \
                               playwright==1.55.0 \
 && pip uninstall -y viewport-agent \
 && rm -rf /tmp/build \
 # Playwright bundles node for its own driver but keeps it off PATH; `lint` wants it.
 && ln -s /usr/local/lib/python3.12/dist-packages/playwright/driver/node /usr/local/bin/node

CMD ["pytest", "-q", "-p", "no:cacheprovider", "agent/tests"]
