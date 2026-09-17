# Mock Reolink NVR for development.
# The official go2rtc image is built on python:*-alpine and already ships go2rtc,
# ffmpeg and Python, so only a font (for the test-pattern labels) and our deps are added.
FROM alexxit/go2rtc:1.9.14

RUN apk add --no-cache font-dejavu

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mock_nvr ./mock_nvr

ENV MOCK_WORKDIR=/tmp/mock-nvr
EXPOSE 80 554
# The base image's tini entrypoint stays in place; this replaces only the command.
CMD ["python3", "-m", "mock_nvr"]
