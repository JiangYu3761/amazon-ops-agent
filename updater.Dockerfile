FROM docker:27-cli
RUN apk add --no-cache git openssh-client python3 curl gzip docker-cli-compose
WORKDIR /opt/amazon-ops-agent
CMD ["sh", "scripts/update_worker.sh"]

