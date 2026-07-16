---
id: 2
tags: [homelab]
created_at: '2026-07-03T11:00:00Z'
updated_at: '2026-07-16T09:30:00Z'
---

# Docker Services

Running services on `pve01` (see [[Home Network Map]] for host details). All managed with Docker Compose, data volumes on the NAS over NFS.

## Services

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| relay | `ghcr.io/pierdom/relay` | 8000 | This knowledge hub |
| vaultwarden | `vaultwarden/server` | 8001 | Password manager (Bitwarden-compatible) |
| miniflux | `miniflux/miniflux` | 8002 | RSS reader |
| immich | `ghcr.io/immich-app/immich-server` | 2283 | Photo library |
| home-assistant | `homeassistant/home-assistant` | 8123 | Home automation |
| grafana | `grafana/grafana` | 3000 | Dashboards |
| prometheus | `prom/prometheus` | 9090 | Metrics scraper |

Reverse proxy: Caddy on the gateway, certs via Let's Encrypt DNS challenge (Cloudflare API token in `relay_secrets`).

## Compose layout

```
/opt/docker/
├── relay/          docker-compose.yml + .env
├── vaultwarden/
├── miniflux/
├── immich/
├── hass/
└── monitoring/     grafana + prometheus + node-exporter
```

## Update procedure

```bash
cd /opt/docker/<service>
docker compose pull && docker compose up -d
docker image prune -f
```

## Change log

### 2026-07-16
Added Immich for photo backups — migrated from Google Photos.

### 2026-07-10
Grafana + Prometheus stack up. Node-exporter on each host.

### 2026-07-03
Initial stack. relay, vaultwarden, miniflux, home-assistant.
