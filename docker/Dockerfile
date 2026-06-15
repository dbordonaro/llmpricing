# LLM Pricing Mirror — Docker
# Serves a mirror of https://sanand0.github.io/llmpricing/
# Auto-updates from the upstream GitHub repo weekly via cron.
# For code-change support (optional):
#   Set GITHUB_TOKEN, FORK_OWNER, GIT_USER_NAME, GIT_USER_EMAIL at runtime.

FROM python:3.13-alpine3.21

# Install system deps: nginx, git, cron, bash
RUN apk add --no-cache nginx git bash dcron curl

ARG GIT_USER_NAME
ARG GIT_USER_EMAIL
RUN if [ -n "$GIT_USER_NAME" ]; then \
        git config --global user.name "$GIT_USER_NAME"; \
    fi \
    && if [ -n "$GIT_USER_EMAIL" ]; then \
        git config --global user.email "$GIT_USER_EMAIL"; \
    fi

# Create directories
RUN mkdir -p /var/www/html /var/log/nginx /run/nginx /app /app/repo

# Copy entrypoint, update script, and nginx config
COPY docker/entrypoint.sh /app/entrypoint.sh
COPY docker/update.sh /app/update.sh
COPY docker/nginx.conf /etc/nginx/http.d/default.conf

RUN chmod +x /app/entrypoint.sh /app/update.sh

EXPOSE 80

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD curl -sf http://localhost:80/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
