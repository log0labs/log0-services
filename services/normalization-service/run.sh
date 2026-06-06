#!/usr/bin/env bash
# Loads .env (if present) into the environment, then runs the service.
# Usage: ./run.sh   (from this service directory)
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a; . ./.env; set +a
  echo "Loaded .env"
else
  echo "No .env found - running with current environment"
fi
exec ./mvnw spring-boot:run
