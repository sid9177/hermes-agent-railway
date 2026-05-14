FROM alpine:3.19
RUN apk add --no-cache curl
CMD ["curl", "-sf", "${HERMES_URL}/api/cron/wake"]