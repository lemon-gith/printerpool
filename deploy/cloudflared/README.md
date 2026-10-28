# cloudflared files (optional)

Only needed if you want **cloudflared to run as a container next to PrintPool**. If cloudflared already runs
on your machine, ignore this folder: point its ingress at `http://localhost:8000`, and its credentials stay
where they are (usually `~/.cloudflared/` or `/etc/cloudflared/`).

To use the container instead:

1. Copy `config.example.yml` to `config.yml` and fill in your tunnel ID and hostname. Keep the service as
   `http://app:8000` (inside Docker, `localhost` would be the cloudflared container itself).
2. Copy your tunnel's credentials file (`~/.cloudflared/<tunnel-id>.json`) into this folder.
   The `cert.pem` from `cloudflared tunnel login` isn't needed to *run* a tunnel, only to create or manage
   tunnels, so it can stay where it is.
3. Make the files readable by the container, which runs as a non-root user (uid 65532):
   `chmod 644 deploy/cloudflared/config.yml deploy/cloudflared/*.json`
   This means other accounts on the machine could read the tunnel credentials too. That's fine on a
   personal server; on a shared one, `chown 65532 deploy/cloudflared/*.json && chmod 600 deploy/cloudflared/*.json` instead.
4. Run `deploy/personal-setup.sh <domain>`, which notices `config.yml` and starts the container.

Everything in this folder except this README and the example is git-ignored.
