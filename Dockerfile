# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /code

COPY ./pyproject.toml ./README.md ./uv.lock* ./

COPY ./app ./app

RUN uv sync --frozen

# The pipeline writes one thing to disk and everything depends on it: the
# database holding the categories, what has been written, and the fact registry
# with its shelf lives. Mount a volume here or a redeploy forgets everything.
RUN mkdir -p /code/data

# Nothing here needs root, and this runs on a machine that also serves a
# website. The venv is built above and never written to afterwards, which is
# what --no-sync below enforces.
RUN useradd --create-home --uid 10001 writer && chown -R writer:writer /code
USER writer

ARG AGENT_VERSION=0.0.0
ENV AGENT_VERSION=${AGENT_VERSION}
ENV UV_CACHE_DIR=/tmp/uv-cache

# This project is a daily job, not a service: the default command produces one
# article and exits, which is what the systemd timer in deploy/ runs.
#
# The ADK web server is still in here for the playground and for A2A, one
# command away:
#
#   docker run --rm -p 8080:8080 ai-content-writer \
#       uv run --no-sync uvicorn app.fast_api_app:app --host 0.0.0.0 --port 8080
#
EXPOSE 8080

CMD ["uv", "run", "--no-sync", "python", "-m", "app.cli", "run"]
