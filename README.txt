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
  installation. Tokens are stored as hashes in the configured SQL database. The config retains the
  initial plaintext secret for first login; keep it private. If you rotate the
  token in the dashboard, update admin_user/admin_secret in that file too.
  Updates authenticate with these saved credentials before stopping the service.
  Native HTTPS is available in the integration profile; no reverse proxy is installed.
  Older installations retain their existing bootstrap-token setup on update.

Updates and data

  Run update inside the container. Core re-runs the CT script, checks upstream
  GitHub releases, downloads the Maven-hosted JAR and verifies its SHA256.
  Application binaries live in /opt/reposilite. Local data, default SQLite and Maven
  artifacts live in var_data_dir (default /var/lib/reposilite) and remain in place during updates.
  Services and tokens use /etc/conf.d/reposilite. Console logs are in
  /var/log/reposilite; Reposilite also writes logs in its working directory.

  Unattended mode uses PHS_SILENT=1. For host-wide update-apps integration and
  scheduled runs, see AUTO-UPDATES.txt and tools/pve/update-apps.sh. The explicit
  adapter recognises this standalone app while keeping other apps on official
  sources. The unmodified official update-apps cannot discover this uncatalogued
  repo. Official update-lxcs already supports its Alpine OS; it does not update
  the Reposilite JAR. No schedule is enabled by default.

  An exclusive lock prevents concurrent updates. Downloads are verified before
  stopping the service. After a clean stop, a root-only backup is created under
  /opt/reposilite-backups/<timestamp>-<pid>/ with separate application/ and state/
  folders. State includes service configuration, local configuration and the
  default SQLite database plus any WAL/SHM/rollback-journal files. Artifact repositories and
  custom/external databases still require your own backups.

  A backup failure restarts the unchanged application where possible. A failed
  replacement, service start or repository readiness check restores the previous
  JAR, version marker and init script and leaves the service stopped. Checks
  authenticate, compare active repository IDs and list each repository root;
  a working dashboard alone is insufficient. API calls have time limits and
  local probes bypass HTTP proxy environment settings. The state backup is retained;
  the live database is not overwritten automatically because the new release
  might have migrated it. A persistent /etc/reposilite/update-in-progress marker
  blocks automatic startup after failure or interruption, including on reboot.
  Inspect /var/log/reposilite and review the JAR/database state before removing
  that marker. AUTO-UPDATES.txt describes recovery and storage timeout settings.

  To restore state after deciding which application version to run, stop the
  service and copy the required files from state/files/<original-absolute-path>
  back to that original path, preserving ownership/permissions. For example,
  the service configuration is at state/files/etc/conf.d/reposilite. A full
  Proxmox backup remains the recovery path for repositories and external data.

  The data and log directories are mode 0700; OpenRC starts Java with umask 0077.
  SQLite can contain plaintext LDAP/S3 secrets, so keep the whole data directory
  private. Updates also repair permissions on the default SQLite database and
  its sidecars. A custom filesystem mount needs equivalent access controls.

Post-install disk cleanup

  A successful new installation removes the downloaded checksum, configuration
  renderer, intermediate runtime variables and database/seeding input files.
  Core cleanup_lxc also clears APK/package caches at the end of installation.
  After the external SQL preflight finishes, Java 21 is reduced from the full
  JDK to an explicitly retained headless JRE before the service first starts.
  A disposable Alpine/amd64 fixture saved about 104 MiB under /usr; the exact
  saving depends on package versions and existing dependencies.

  The application JAR, native configuration, credentials, database/artifacts,
  TLS/CA files, truststore, update guard, logs and recovery backups remain.
  Python/OpenSSL and Core tools stay available for maintenance and updates.
  The JDK reduction applies to new installations; application updates do not
  remove packages from an existing installation. See TESTING.txt for evidence.

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
  var_integrations_json       {} (native connection/TLS profile)
  var_integrations_file       unset (mode-600 profile on Proxmox host)

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

  This allows per-repository quotas and mounts in Reposilite's native format.
  Filesystem quota overrides accept 1-100%, positive byte counts, or KB/MB/GB
  sizes such as 500MB. B/TB suffixes are unsupported by Reposilite 3.6.5.
  Values exceeding the signed 64-bit byte limit are rejected.
  Validation rejects overlapping S3 namespaces and malformed provider settings
  before creating a container. The API validates the remaining schema at installation;
  ensure custom mount paths are writable. Avoid putting secrets in a shared
  command. Repository and dashboard defaults are seeded into the configured SQL database once using
  the manager API. They remain editable in the dashboard and are not reapplied
  by update. Changing the original var_* inputs later does not reconfigure an
  existing installation; use the dashboard or edit /etc/conf.d/reposilite.

  Filesystem repositories must use separate, non-overlapping directories.
  Relative mounts stay below <data_directory>/repositories; absolute mounts
  must be repository subdirectories or dedicated directories under /srv or
  /mnt, separate from private application data. Installation checks normalized
  paths and symlink targets in the container, including default repository paths.
  Never map a public and private repository to the same directory or nested
  directories. Existing mounts are not migrated; keep this rule when editing
  settings in the dashboard too. Bind-mount aliases and aliases of remote S3
  endpoints may refer to the same storage despite having different paths/URLs.

Native DB, external storage, LDAP and TLS

  Advanced Settings -> Customize Reposilite application settings -> Configure
  database, external storage, LDAP and native TLS opens a guided TTY setup.
  Passwords are entered with hidden input. Environment-supplied profiles skip
  the guided setup. No reverse proxy or OIDC gateway is installed.

  For unattended installation, copy examples/integrations.json, replace the
  example endpoints/secrets and remove sections you do not need. Protect it:

    chmod 600 /root/reposilite-profile.json
    var_integrations_file=/root/reposilite-profile.json mode=generated bash -c "$(curl -fsSL https://raw.githubusercontent.com/hwajin-me/proxmox-reposilite/main/ct/reposilite.sh)"

  Add your Core storage/network choices as usual. File contents are validated
  before container creation and transferred through the Core install environment.
  Do not put secret JSON directly into shared commands, logs or source control.
  Keep the source profile private or remove it after provisioning.

  Database:
    SQLite is the default. MariaDB, MySQL and PostgreSQL accept host, port,
    name, user, password and an optional parameters object containing JDBC driver
    options. MariaDB is supported upstream; MySQL/PostgreSQL are labelled
    experimental by Reposilite. Create the database and database user first;
    this installer does not provision an external SQL server or migrate data.
    MariaDB uses its native ENV credential provider and a root-only
    /etc/reposilite/database.env file, preserving spaces and special characters.
    MySQL/PostgreSQL credentials are URL-encoded into the root-only native
    configuration file. Database passwords are never Java command arguments. TLS options depend on the selected driver,
    e.g. parameters={"sslMode":"verify-full"} for MariaDB or PostgreSQL's
    {"sslmode":"verify-full"}. Supply trustedCertificates for private CAs.
    External databases require independent, consistent backups before update.
    Use a dedicated, empty database on first install: settings are seeded once.
    Before starting Reposilite, a Java 21/JDBC metadata check verifies the
    connection and refuses existing user relations, including tables, partitioned
    tables, views and materialized views. The database account must be able to
    enumerate all relations. This check does not create tables or adopt an
    existing Reposilite database. Connection details and errors are kept private.
    For PostgreSQL with trustedCertificates, the renderer selects the JVM SSL
    factory so those CAs are used, unless sslfactory/sslrootcert is explicit.

  Artifact storage:
    Local filesystem remains the default. S3 profiles support AWS S3, MinIO,
    R2 and other compatible endpoints, bucketName, region, accessKey, secretKey,
    prefix, signer, pathStyle and skipBucketCreation. The profile always uses
    sharedBucket=true to isolate repositories as <prefix>/<repository>/....
    R2 needs region=auto and signer=LEGACY_V4. Typical MinIO uses pathStyle=true.
    For an existing restricted bucket, use skipBucketCreation=true (default in
    the guided setup). S3 credentials are stored in Reposilite's settings DB.
    This mode requires Reposilite 3.6.5 or newer for shared-bucket isolation.
    var_repositories_json overrides the profile storage and allows mixed fs/S3
    repositories; give each S3 repository an isolated bucket/prefix or enable
    sharedBucket. Global path-style/bucket-creation flags still come from the
    profile. Moving existing artifacts between providers is manual.

    For host-mounted NFS/SMB storage, use storage={"type":"fs","root":"/mnt/artifacts"}.
    First mount it on Proxmox and bind it into the LXC at that exact path, with
    writable ownership for the unprivileged container. The path must already be
    a mount point before installer setup. The script checks it during installation
    and at every service start, so a missing mount cannot silently fill the root
    disk. It does not mount NFS/SMB inside the unprivileged LXC. Provision such a
    bind mount through your host/container setup before running the application
    installer; the normal fresh-container wizard does not create this bind mount.

  LDAP / LDAPS (native Reposilite authentication):
    Set ldap.enabled=true, hostname, ssl, port, baseDn, searchUserDn and
    searchUserPassword. Optional typeAttribute (objectClass value, default person),
    userAttribute (default uid), userFilter (additional LDAP filter, default empty)
    and userType (TEMPORARY by default, or PERSISTENT) map directly to Reposilite's
    authentication settings. Use a read-only search account. LDAPS is the guided
    default (port 636); plain LDAP uses port 389. StartTLS is not supported by
    Reposilite's current authenticator. An enterprise/private LDAP CA can be
    supplied in trustedCertificates as PEM strings; guided setup reads CA files
    from the Proxmox host and imports them into a private JVM truststore while
    preserving system CAs. Certificate/hostname verification stays enabled.

    LDAP users authenticate with their directory username/password and receive
    mapped access tokens. They do not automatically receive manager or repository
    write permissions: grant the needed token permissions in the dashboard.
    The generated local manager remains available for recovery. If the default
    manager name admin is used with LDAP, it becomes local-manager-<random>.
    New installations add an LDAP user-filter exclusion for the initial local
    manager, including a custom manager name, while preserving your filter.
    Reserve all other privileged token names so they never match directory
    usernames: upstream LDAP reuses existing tokens by name, including their
    permissions. Updates preserve existing LDAP settings; installations made
    before this exclusion must add it in the dashboard themselves. For uid and
    manager maintainer, combine your filter with (!(uid=maintainer)) using AND.
    There is no automatic LDAP group-to-permission mapping.
    Upstream token
    authentication can accept a previously issued persistent token independently
    of LDAP; choose mapped token type deliberately and review token revocation.
    OIDC has no native setting in the verified Reposilite release and is excluded.

  Native TLS:
    tls.mode is off (default), self-signed or provided; set hostname and optional
    port (default 443). HTTP redirects to HTTPS when enabled. Self-signed mode
    generates an RSA certificate with a matching DNS/IP SAN, valid for 365 days.
    Trust that certificate on clients or replace it with a trusted certificate.
    Provided mode accepts certificate (fullchain PEM) and privateKey (unencrypted
    PEM) strings; the guided setup reads these from host files. The key pair,
    DNS/IPv4 hostname and expiration are checked before application startup. Set DNS to
    the container IP. The installer and updater verify HTTPS with the configured
    certificate and local hostname resolution; they do not disable TLS checks.
    Certificates/keys live under /etc/reposilite/tls with private permissions.
    Renew externally, replace certificate.pem and private-key.pem, and restart
    Reposilite. Automatic ACME issuance/renewal is not included.

  The native config is /etc/reposilite/configuration.cdn (mode 600). Shared
  repository and LDAP settings are seeded through the manager API into the
  configured database. Temporary storage/LDAP JSON copies are removed after
  setup. Updates preserve settings and certificates; they do not reapply profiles
  or migrate an existing SQLite database to an external database. Profile-based
  configuration is for new installs. Existing instances can use the dashboard
  for LDAP/repositories and edit native config/service settings for DB/TLS.

  JSON metadata describes application variables for catalog integration.
  Credential-bearing JSON inputs are marked secret; service configuration values
  are shell-escaped. Application configuration code disables shell tracing.

  ARM64 and Incus are not tested. Core asks before attempting unverified ARM64.
  See TESTING.txt for validation scope, AUDIT.txt and COMPATIBILITY.txt for the
  reviews, and PR-DRAFT.txt for the prepared submission text. This preparation
  did not create or update a PR.

Sources
  https://reposilite.com/guide/general
  https://reposilite.com/guide/tokens
  https://reposilite.com/guide/ldap
  https://reposilite.com/guide/s3
  https://reposilite.com/guide/ssl
  https://github.com/dzikoysk/reposilite
  https://community-scripts.org/docs

License: MIT
