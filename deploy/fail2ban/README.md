# fail2ban for the Climate Monitor Wiki host

Config as deployed on the host. Mirrored here for version control — the live
files are `/etc/fail2ban/jail.local` and `/etc/fail2ban/filter.d/caddy-*.conf`.

**Fail2Ban v1.1.0**, 4 jails: `caddy-json`, `caddy-notfound`, `sshd`, `recidive`.

## Install

```bash
sudo apt-get install -y fail2ban
sudo cp deploy/fail2ban/jail.local          /etc/fail2ban/jail.local
sudo cp deploy/fail2ban/filter.d/*.conf     /etc/fail2ban/filter.d/
sudo fail2ban-client -t          # validate before restarting
sudo systemctl restart fail2ban
sudo systemctl enable fail2ban
```

---

## The critical bit: `chain = DOCKER-USER`

**Default fail2ban settings do not work with Docker.** This is the single most
common way a fail2ban install ends up as decoration.

Caddy runs in Docker with published ports (80/443). Docker attaches its rules to
the **FORWARD** path, so packets for a published container port **never traverse
the INPUT chain**. Fail2ban's default `chain = INPUT` therefore inserts rules
that match nothing:

- `fail2ban-client status` reports the IP as banned ✅
- `/var/log/fail2ban.log` shows the ban ✅
- the attacker keeps browsing the site, completely unaffected ❌

No error is raised anywhere. Verified on this host: with the container running,
the INPUT chain is empty while all site traffic flows through FORWARD.

Docker provides `DOCKER-USER` for exactly this. It is evaluated in FORWARD
*before* Docker's own rules, and Docker never flushes it, so bans survive
container restarts.

`sshd` is the exception — it's a host service, not containerized, so that jail
correctly uses `chain = INPUT`.

### Verify bans are real, not just recorded

```bash
sudo iptables -L DOCKER-USER -n --line-numbers   # jump rule must be here
sudo iptables -L f2b-caddy-json -n               # actual REJECT rules
```

If bans only ever appear under `iptables -L INPUT`, the `chain` setting was lost
and **nothing is actually being blocked.**

---

## Filter design

Filters parse Caddy's **JSON** access log directly rather than using the
`transform-encoder` "common log" plugin, which would require rebuilding Caddy
with `xcaddy` — not possible with the stock `caddy:2-alpine` image.

`datepattern = LongEpoch` matches Caddy's float epoch (`"ts":1786397145.32`).
A wrong datepattern is the second-most-common silent failure: lines match the
regex but get no usable timestamp, so `findtime` never accumulates.

**`caddy-json`** — high-signal, low threshold (4 in 10m → 2h):
- `401`/`403` auth failures
- probes for `/.git`, `/.env`, `/wp-admin`, `/xmlrpc.php`, `/.aws`, `/.ssh`, etc.
- scanner User-Agents (`sqlmap`, `nikto`, `nmap`, `masscan`, `wpscan`, …)

**`caddy-notfound`** — deliberately loose (40 in 2m → 1h). A blanket "ban any
404" rule is **intentionally avoided**: this is a single-page app whose frontend
can legitimately 404 on assets mid-deploy, and one broken link should never ban
a real visitor. 40 misses in 2 minutes is machine-speed enumeration no human
produces.

**`recidive`** — 3 bans from any jail within a day → 1 week.

Progressive banning is on globally (`bantime.increment`, factor 2, cap 7d), so
repeat offenders escalate while a one-off mistake costs an hour.

### Whitespace tolerance

Every regex uses `\s*` after JSON colons. Caddy emits compact JSON, but any
re-serialization (jq, a log shipper, a test fixture) adds spaces, and a
compact-only regex fails **silently**. This was caught during testing: the first
version scored 0/5 against spaced JSON.

---

## Whitelist

`ignoreip` covers loopback, `172.16.0.0/12`, `192.168.0.0/16`, `10.0.0.0/8`.

This matters operationally: `weekly_wiki_refresh.sh` health-checks the public
HTTPS URL **from the host itself**, and `/api/reload` returns 403 without a
token. Without the whitelist the pipeline could ban the very host it runs on.

Verified: 8 real 403s generated from the host produced **0 counted failures**.

> `fail2ban-client set <jail> banip <ip>` bypasses `ignoreip` by design — a
> manual override is taken as deliberate. Whitelist behaviour must be tested
> through the log-detection path, not via a manual ban.

---

## Verification performed

Every claim above was tested on the live host, not assumed:

| Check | Result |
|---|---|
| `fail2ban-client -t` | OK |
| Filters vs. real access log | 2/2 real 403s matched |
| Compact JSON (Caddy's actual format) | 5/5 matched |
| Spaced JSON (re-serialized) | 5/5 matched after `\s*` fix |
| Ban lands in `DOCKER-USER`, not `INPUT` | confirmed, INPUT count 0 |
| REJECT rule present in `f2b-caddy-json` | confirmed |
| End-to-end: 5 injected scanner hits | detected → auto-banned |
| Whitelist: 8 host-origin 403s | 0 failures counted |
| Test artifacts removed | 0 banned, log cleaned |
| Enabled at boot | enabled |
| Site still healthy | `/api/config` → 200 |

## Operations

```bash
sudo fail2ban-client status                    # all jails
sudo fail2ban-client status caddy-json         # one jail
sudo fail2ban-client set caddy-json unbanip <IP>
sudo tail -f /var/log/fail2ban.log
sudo fail2ban-regex <logfile> /etc/fail2ban/filter.d/caddy-json.conf
```

## Known limitation

The current setup bans on **`remote_ip`**, which is correct today because Caddy
is edge-facing. If a CDN or upstream proxy is ever placed in front, every
request will carry the proxy's IP and a single ban could block all users. In
that case switch the filters to `client_ip` and configure Caddy's
`trusted_proxies`.
