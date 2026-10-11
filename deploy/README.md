# Deploy on a Linux server

1. On the server, as root: `git clone <repo> /opt/aerchain && bash /opt/aerchain/deploy/setup.sh`
   (installs Python and Chrome, creates the `aerchain` user and a systemd service; `HOST`/`PORT` can be set).
2. Put secrets in `/etc/aerchain/aerchain.env` (root-only, never in git):
   `OPENAI_API_KEY`, `SMTP_*`, `AERCHAIN_PUBLIC_URL=https://your-address`, and `AERCHAIN_BUYER_PASSWORD` (the login for
   everything except the vendor upload page `/q/<token>`). Then `systemctl restart aerchain`.
3. Put a reverse proxy with HTTPS in front: see `nginx.example.conf`.
4. Update: `cd /opt/aerchain && git pull`, then `systemctl restart aerchain`.

Notes: run one worker only (state is local files). State lives in `/opt/aerchain/state`: back it up.
The buyer login is HTTP Basic (user `buyer`), so serve the app over HTTPS only.
