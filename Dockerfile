# Container image for MCP directory scanners (Glama and similar).
#
# This server does its real work on Windows only: UI Automation and PrintWindow
# have no Linux equivalent. The image exists so a registry can install the
# package, start it and read its tool list; every tool call answers with a clear
# "requires Windows" message instead of crashing, and pyproject.toml declares
# uiautomation with a sys_platform == 'win32' marker so it is not installed here.
#
# The Windows code path is unaffected: on Windows this image is pointless, run
# `uvx som-window-mcp` instead.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

ENTRYPOINT ["som-window-mcp"]
