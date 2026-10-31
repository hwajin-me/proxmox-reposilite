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
  Manager token name: admin (customizable)
  Installation creates a persistent manager token, verifies it, removes the
  temporary startup token and verifies login again after a service restart.
  Read admin_user and admin_secret in the root-only service configuration:

    pct exec <CTID> -- cat /etc/conf.d/reposilite

  The service starts without a token secret in its process arguments after
  installation. Tokens are stored as hashes in SQLite. The config retains the
  initial plaintext secret for first login; keep it private. If you rotate the
  token in the dashboard, update your saved credentials accordingly.
  Use HTTPS through your reverse proxy when accessing from untrusted networks.
  Older installations retain their existing bootstrap-token setup on update.

Updates and data

  Run update inside the container. Core re-runs the CT script, checks upstream
  GitHub releases, downloads the Maven-hosted JAR and verifies its SHA256.
  Application binaries live in /opt/reposilite. Configuration, SQLite and Maven
  artifacts live in var_data_dir (default /var/lib/reposilite) and remain in place during updates.
  Services and tokens use /etc/conf.d/reposilite. Console logs are in
  /var/log/reposilite; Reposilite also writes logs in its working directory.

  An exclusive lock prevents concurrent updates. Downloads are verified before
  stopping the service. After a clean stop, a root-only backup is created under
  /opt/reposilite-backups/<timestamp>-<pid>/ with separate application/ and state/
  folders. State includes service configuration, local configuration and the
  default SQLite database plus any WAL/SHM files. Artifact repositories and
  custom/external databases still require your own backups.

  A backup failure restarts the unchanged application where possible. A failed
  replacement, service start or HTTP health check restores the previous JAR and
  version marker and leaves the service stopped. The state backup is retained;
  the live database is not overwritten automatically because the new release
  might have migrated it. Inspect /var/log/reposilite before recovery.

  To restore state after deciding which application version to run, stop the
  service and copy the required files from state/files/<original-absolute-path>
  back to that original path, preserving ownership/permissions. For example,
  the service configuration is at state/files/etc/conf.d/reposilite. A full
  Proxmox backup remains the recovery path for repositories and external data.

Community Scripts Core compatibility

  This standalone distribution uses the Community Scripts Core lifecycle and
  includes json/reposilite.json with application-variable metadata. No upstream
  PR is active. The official generator lists catalogued scripts; the commands
  in this repository work directly.
  Earlier alpine-reposilite install URLs redirect to the reposilite script;
  existing configuration is preserved.

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

  Repository names are comma separated; surrounding whitespace is ignored. Use - for an empty
  public/private list. IDs must be unique across both lists. The quota is the
  maximum percentage of available filesystem space per repository; it does
  not reserve capacity or sum to a combined quota. Use yes/no for switches.
  var_data_dir accepts a path below /var/lib or /srv containing letters,
  digits, slashes, underscores and hyphens. It is separate from the JAR path.
  The installer requires at least 128 MiB of container RAM above var_java_heap.

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
