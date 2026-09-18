FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies only, pinned by hash, in their own layer: the image is reproducible and
# editing the agent does not reinstall anything. Refresh the lock from the repository root,
# on the same image as the FROM line above so the pins are resolved on the Python that runs
# them:
#   docker run --rm -v "$PWD/agent:/src" -w /src "$(sed -n 's/^FROM //p' agent/Dockerfile)" sh -c \
#     "pip install -q pip-tools && pip-compile --generate-hashes -o requirements.lock.txt pyproject.toml"
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt

# Run from source, so agent/viewport/web can be bind-mounted during development.
COPY pyproject.toml ./
COPY viewport ./viewport
RUN useradd --system --uid 10001 viewport \
 # COPY keeps the build context's file modes. A restrictive umask on the
 # developer's machine would otherwise leave /app unreadable to `viewport`.
 && chmod -R a+rX /app \
 # Writable home for admin-page edits. Creating it here (owned by `viewport`) means a
 # named volume mounted over it inherits that ownership instead of being root-only.
 && mkdir -p /data && chown viewport:viewport /data

USER viewport
EXPOSE 8080
CMD ["python", "-m", "viewport"]
