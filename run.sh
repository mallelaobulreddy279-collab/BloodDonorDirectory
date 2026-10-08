#!/usr/bin/env bash
cd "$(dirname "$0")/app" && python3 server.py "$@"
