FROM docker:27-cli
RUN apk add --no-cache git python3 curl docker-cli-compose
# Compose mounts the checkout at its actual host path, so daemon-created bind
# mounts resolve consistently regardless of the user's chosen project directory.
CMD ["sh", "scripts/update_worker.sh"]
