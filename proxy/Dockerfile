# The front door: routes each path to its tool and asks the portal whether each request is allowed.
FROM caddy:2.8.4-alpine
COPY Caddyfile /etc/caddy/Caddyfile
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
