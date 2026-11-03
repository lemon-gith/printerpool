# cloudflared files (locally managed tunnels)

Only needed with `[cloudflared] mode = "config"` in `printpool.toml`, i.e. a tunnel you manage yourself with a
`config.yml`. For a tunnel managed in the Cloudflare dashboard, use `mode = "token"` and put the token in `.env`
as `CLOUDFLARE_TUNNEL_TOKEN` instead.

1. Copy `config.example.yml` to `config.yml` and fill in your tunnel ID and hostname. Keep the service as
   `http://gateway:8080`: that's PrintPool's gateway on the Docker network.
2. Copy your tunnel's credentials file (`~/.cloudflared/<tunnel-id>.json`) into this folder. The `cert.pem` from
   `cloudflared tunnel login` isn't needed to *run* a tunnel, only to create or manage tunnels.
3. Make the files readable by the cloudflared container, which runs as a non-root user:
   `chmod 644 deploy/cloudflared/config.yml deploy/cloudflared/*.json`
4. Run `deploy/setup-server.sh` (or `.ps1`).

Everything in this folder except this README and the example is git-ignored.
