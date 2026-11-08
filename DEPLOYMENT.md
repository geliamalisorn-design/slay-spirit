# Docker deployment

The app uses Python's standard library, so the image does not install third-party runtime packages. SQLite is stored in a named Docker volume and survives container rebuilds and restarts.

## Run with Docker Compose

Install Docker Engine and the Docker Compose plugin on the host, then run:

```sh
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

Open `http://localhost:8000`. To choose another port or bind address, copy `.env.example` to `.env` and edit `APP_PORT` and `BIND_ADDRESS` before starting Compose. The default bind address is `127.0.0.1`, suitable when a reverse proxy on the same host terminates HTTPS. Set `BIND_ADDRESS=0.0.0.0` only when the host firewall and network policy are configured for direct access.

## Deploy on a server

1. Install Docker Engine and the Compose plugin on a Linux server.
2. Copy or clone the project to the server and configure `.env` if needed.
3. Put a TLS reverse proxy such as Caddy or nginx in front of `127.0.0.1:8000`. Forward the original `X-Forwarded-Proto` header so login cookies are marked secure over HTTPS.
4. Start the app with `docker compose up -d --build` and verify it with `docker compose ps` and `docker compose logs app`.
5. Back up the `slay-data` volume regularly. For example, stop writes briefly and archive the volume, or use SQLite's online backup API. Keep backups outside the server.

To deploy an updated version, fetch the new source and run `docker compose up -d --build`. This keeps the database volume. To remove containers while retaining user data, run `docker compose down`. `docker compose down -v` also deletes the database volume and all registered accounts and game history.

## Build and run without Compose

```sh
docker build -t slay-investments .
docker run -d --name slay-investments --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v slay-data:/var/lib/slay \
  slay-investments
```

The container listens on port `8000`. Configure `PORT` and `SLAY_DB_PATH` only if you also update the published port and persistent mount accordingly.
