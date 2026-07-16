---
id: 7
tags: [inbox]
created_at: '2026-07-16T11:00:00Z'
---

# WireGuard on iOS setup

Quick notes from setting up the WireGuard client on iPhone. Move to `homelab` once complete.

- Download WireGuard app from App Store
- Export config from `pve01` (`wg genkey | tee privatekey | wg pubkey > publickey`)
- Scan QR code — `wg showconf wg0 | qrencode -t ansi`
- Test: `ping 192.168.1.1` from mobile data (not Wi-Fi)

TODO: add the phone's public key to `wg0.conf` on `pve01` and reload (`wg syncconf`).
