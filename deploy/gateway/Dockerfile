# The gateway: nginx, configured by .generated/gateway.conf (rendered from printpool.toml).
FROM nginx:stable-alpine
# Copied (not mounted) so it's executable on every host, Windows included.
COPY resolver.sh /docker-entrypoint.d/40-printpool-resolver.sh
RUN chmod 755 /docker-entrypoint.d/40-printpool-resolver.sh
