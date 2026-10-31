Reposilite on Alpine LXC for Proxmox VE

Install from the Proxmox VE host shell:

  bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/alpine-reposilite.sh)"

Community Scripts Core provides the normal container wizard. Choose your own
container ID, storage, bridge, DNS and networking. No host-specific settings or
credentials are included. Defaults: Alpine 3.24, unprivileged LXC, 1 CPU,
1024 MiB RAM, 8 GiB disk. Java 21 and Reposilite run directly with OpenRC.

Unattended example (uses Core's app defaults and DHCP; adapt to your host):

  mode=generated var_container_storage=local-lvm var_template_storage=local bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/alpine-reposilite.sh)"

Access and first login

  Dashboard: http://<container-ip>:8080
  Initial manager token name: admin
  Read its random secret from /etc/conf.d/reposilite inside the container:

    pct exec <CTID> -- cat /etc/conf.d/reposilite

  The initial token is temporary in Reposilite's terminology: the service
  recreates it from bootstrap_secret on each start. For a permanent manager
  token, run this in Dashboard > Console:

    token-generate administrator m

  Save the resulting secret, clear bootstrap_secret in /etc/conf.d/reposilite,
  then run rc-service reposilite restart. The permanent token remains in SQLite.
  Use HTTPS through your reverse proxy when accessing from untrusted networks.

Updates and data

  Run update inside the container. Core re-runs the CT script, checks upstream
  GitHub releases, downloads the Maven-hosted JAR and verifies its SHA256.
  Application binaries live in /opt/reposilite. Configuration, SQLite and Maven
  artifacts live in /var/lib/reposilite and remain in place during updates.
  Services and tokens use /etc/conf.d/reposilite. Console logs are in
  /var/log/reposilite; Reposilite also writes logs in its working directory.

  Each update saves the previous JAR and version marker under
  /opt/reposilite-backups/<timestamp>. This is not a complete data backup.
  Use Proxmox backups for repositories and database. If startup fails after an
  update, the old JAR is restored and the service stays stopped; inspect logs
  and database compatibility before manually restarting it.

Community Scripts generator and contribution

  https://community-scripts.org/generator generates commands for scripts already
  present in its catalog. This script follows the same Core lifecycle and has
  json/alpine-reposilite.json for catalog integration. The public repository can
  be used directly before official review and promotion.
  Official listing is subject to the Community Scripts maintainers' review.

  Source files:
    ct/alpine-reposilite.sh
    install/alpine-reposilite-install.sh
    json/alpine-reposilite.json

  ARM64 and Incus are not tested. Core asks before attempting unverified ARM64.

Sources
  https://reposilite.com/guide/general
  https://reposilite.com/guide/tokens
  https://github.com/dzikoysk/reposilite
  https://community-scripts.org/docs

License: MIT
