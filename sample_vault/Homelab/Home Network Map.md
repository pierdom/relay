---
id: 1
tags: [homelab, reference]
created_at: '2026-07-01T10:00:00Z'
updated_at: '2026-07-15T14:20:00Z'
---

# Home Network Map

Static reference for the home network topology. Update when adding/removing devices or changing subnets.

## Subnets

| CIDR | VLAN | Purpose |
|------|------|---------|
| `192.168.1.0/24` | 1 (default) | Trusted devices — laptops, phones, NAS |
| `192.168.10.0/24` | 10 | Servers — homelab rack, Pi cluster |
| `192.168.20.0/24` | 20 | IoT — lights, plugs, sensors (no internet) |
| `10.8.0.0/24` | — | WireGuard VPN tunnel |

## Key devices

| Hostname | IP | Role |
|----------|----|------|
| `gateway` | 192.168.1.1 | OPNsense router |
| `nas` | 192.168.1.10 | TrueNAS — primary storage |
| `pve01` | 192.168.10.2 | Proxmox — main hypervisor |
| `pve02` | 192.168.10.3 | Proxmox — secondary |
| `pi-hole` | 192.168.10.5 | Pi-hole DNS + DHCP for VLAN 10 |
| `relay` | 192.168.10.20 | This relay instance |

## DNS

Internal domain: `home.arpa`. Pi-hole handles VLAN 10; OPNsense handles VLANs 1 and 20.

External DNS: Cloudflare (`1.1.1.1`), quad9 (`9.9.9.9`) as secondary.

## WireGuard

`pve01` hosts the WireGuard server (`wg0`, UDP 51820). Peers: work laptop, phone. Split-tunnel — only `192.168.0.0/16` routes through the VPN.

## Change log

### 2026-07-15
Added IoT VLAN 20 — separated smart home devices from the trusted subnet.

### 2026-07-01
Initial map. Migrated from flat `/24` to VLAN setup.
