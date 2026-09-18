# Test runner: unit tests and the browser end-to-end check, both in a container.
# The Playwright image ships Chromium, Firefox and WebKit plus their system libraries
# for amd64 and arm64. Only Firefox and WebKit decode H.264/H.265, so the e2e check
# uses Firefox; bundled Chromium would need MJPEG.
#
# The tag below and the playwright== pin further down must name the same release. The image
# carries only its own release's browser builds (firefox-1543 for 1.63), and the Python package
# looks for its browsers by build number, so a mismatch cannot launch anything. Dependabot
# bumps this line alone: the build then stops at the check below with the fix spelled out,
# rather than failing halfway through the browser suite.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /repo

# Dependencies only. Tests run from the bind-mounted source, so editing a test
# or a module needs no rebuild.
COPY agent /tmp/build/agent
COPY mock-nvr/requirements.txt /tmp/build/mock-requirements.txt
RUN pip install --no-cache-dir "/tmp/build/agent[dev]" -r /tmp/build/mock-requirements.txt \
                               playwright==1.63.0 \
 && pip uninstall -y viewport-agent \
 && rm -rf /tmp/build \
 && python3 -c 'import os, sys; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); path = p.firefox.executable_path; p.stop(); sys.exit(0 if os.path.exists(path) else "tests/Dockerfile: the playwright== pin does not match the FROM tag, and this image has no " + path)' \
 # Playwright bundles node for its own driver but keeps it off PATH; `lint` wants it. Found
 # through the package rather than spelled out, so a newer Python in the image moves it too.
 && ln -s "$(python3 -c 'import pathlib, playwright; print(pathlib.Path(playwright.__file__).parent / "driver" / "node")')" /usr/local/bin/node

CMD ["pytest", "-q", "-p", "no:cacheprovider", "agent/tests"]
