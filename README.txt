Reposilite on Alpine LXC for Proxmox VE

Install from the Proxmox VE host shell:

  bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/reposilite.sh)"

Community Scripts Core provides the normal container wizard. Choose your own
container ID, storage, bridge, DNS and networking. No host-specific settings or
credentials are included. Defaults: Alpine 3.24, unprivileged LXC, 1 CPU,
1024 MiB RAM, 8 GiB disk. Java 21 and Reposilite run directly with OpenRC.

Unattended example (uses Core's app defaults and DHCP; adapt to your host):

  mode=generated var_container_storage=local-lvm var_template_storage=local bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/reposilite.sh)"

Access and first login

  Dashboard: http://<container-ip>:<configured-port> (default 8080)
  Initial manager token name: admin (customizable)
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
  artifacts live in var_data_dir (default /var/lib/reposilite) and remain in place during updates.
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
  json/reposilite.json for catalog integration. The public repository can
  be used directly before official review and promotion.
  Official listing is subject to the Community Scripts maintainers' review.
  Contribution PR: https://github.com/community-scripts/DevScripts/pull/2489

  Source files:
    ct/reposilite.sh
    install/reposilite-install.sh
    json/reposilite.json

  Choose Advanced Settings in the Core wizard, then answer yes to
  "Customize Reposilite application settings?" to set application defaults.
  Values supplied through var_* environment variables are kept as provided.
  Default / generated installs never wait for application prompts.

Application settings

  Variable                    Default
  var_port                    8080
  var_data_dir                /var/lib/reposilite
  var_java_heap               256 (MiB)
  var_admin_user              admin
  var_public_repositories     releases,snapshots
  var_private_repositories    private
  var_repository_quota        100 (percent, per repository)
  var_redeployment            no
  var_preserve_snapshots      no
  var_dashboard_title         Reposilite Repository
  var_dashboard_description   Public Maven repository hosted through Reposilite
  var_repositories_json       unset (optional advanced override)

  Repository names are comma separated, without spaces. Use - for an empty
  public/private list. IDs must be unique across both lists. The quota is the
  maximum percentage of available filesystem space per repository; it does
  not reserve capacity or sum to a combined quota. Use yes/no for switches.
  var_data_dir accepts a path below /var/lib or /srv containing letters,
  digits, slashes, underscores and hyphens. It is separate from the JAR path.
  Adjust the container RAM to leave headroom above var_java_heap.

  Example (add your host-specific storage/network choices):

    mode=generated var_port=9090 var_data_dir=/srv/maven var_java_heap=512 var_admin_user=maintainer var_public_repositories=stable,nightly var_private_repositories=internal var_repository_quota=80 var_redeployment=yes var_dashboard_title='Team Maven' bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/reposilite.sh)"

  var_repositories_json accepts the Reposilite repositories array, overriding
  the public/private lists, quota and repository switches. For example:

    [{"id":"stable","visibility":"PUBLIC","storageProvider":{"type":"fs","quota":"60%","mount":""},"redeployment":false,"preserveSnapshots":false,"proxied":[]},{"id":"internal","visibility":"PRIVATE","storageProvider":{"type":"fs","quota":"20%","mount":""},"redeployment":true,"preserveSnapshots":true,"proxied":[]}]

  This allows per-repository quotas, mounts and upstream proxy definitions in
  Reposilite's native format. The API validates the full schema at installation;
  ensure custom mount paths are writable. Avoid putting secrets in a shared
  command. Repository and dashboard defaults are seeded into SQLite once using
  the manager API. They remain editable in the dashboard and are not reapplied
  by update. Changing the original var_* inputs later does not reconfigure an
  existing installation; use the dashboard or edit /etc/conf.d/reposilite.

  JSON metadata describes every supported app variable for catalog integration.

  ARM64 and Incus are not tested. Core asks before attempting unverified ARM64.

Sources
  https://reposilite.com/guide/general
  https://reposilite.com/guide/tokens
  https://github.com/dzikoysk/reposilite
  https://community-scripts.org/docs

License: MIT
