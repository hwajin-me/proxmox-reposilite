#!/usr/bin/env python3
"""Validate install profiles and render native Reposilite configuration. No proxy."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess
import sys
from urllib.parse import quote, urlencode, urlsplit


class ProfileError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ProfileError(message)


def obj(value, fields, label):
    require(isinstance(value, dict), f"{label} must be an object")
    require(not set(value) - set(fields), f"Unknown {label} field")
    return value


def text(value, label, empty=False):
    require(isinstance(value, str) and (empty or bool(value)) and
            not any(ord(c) < 32 for c in value), f"Invalid {label}")
    return value


def port(value, label):
    require(type(value) is int and 1 <= value <= 65535, f"Invalid {label}")
    return value


def host(value):
    text(value, "hostname")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", value)), "Invalid hostname (DNS name or IPv4 required)")
    return value


def url(value):
    parsed = urlsplit(text(value, "endpoint"))
    require(parsed.scheme in ("http", "https") and parsed.hostname and
            not parsed.username and not parsed.password and not parsed.query and
            not parsed.fragment and parsed.path in ("", "/"), "Invalid S3 endpoint")
    if parsed.port is not None:
        port(parsed.port, "S3 endpoint port")


def ldap_filter(value):
    text(value, "LDAP filter", empty=True)
    if not value:
        return
    depth = 0
    index = 0
    for i, character in enumerate(value):
        if i < index:
            continue
        if character == "\\":
            require(bool(re.fullmatch(r"[0-9a-fA-F]{2}", value[i + 1:i + 3])), "LDAP filter escapes require two hexadecimal digits")
            index = i + 3
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            require(depth >= 0 and (depth != 0 or i == len(value) - 1), "LDAP filter must contain one balanced filter")
    require(value.startswith("(") and value.endswith(")") and depth == 0, "LDAP filter must contain one balanced filter")


def filesystem_location(name, mount, root=None):
    def canonical(path):
        path = posixpath.normpath(path)
        if root is None:
            return path
        anchor = root.resolve()
        try:
            resolved = (anchor / path.lstrip("/")).resolve()
        except (OSError, RuntimeError):
            raise ProfileError("Could not resolve filesystem mount") from None
        require(resolved == anchor or anchor in resolved.parents, "Filesystem mount escapes the container root")
        return "/" if resolved == anchor else "/" + resolved.relative_to(anchor).as_posix()

    data = canonical(os.environ.get("var_data_dir", "/var/lib/reposilite"))
    base = posixpath.join(data, "repositories")
    location = canonical(posixpath.join(base, mount or name))
    internal = location.startswith(base + "/")
    external = location.startswith(("/srv/", "/mnt/")) and not (
        location == data or location.startswith(data + "/") or data.startswith(location + "/"))
    require(internal or external, "Filesystem mounts must use repository subdirectories or separate artifact directories under /srv or /mnt")
    return location


def validate_quota(value):
    require(isinstance(value, str), "Filesystem quota must be a string")
    if re.fullmatch(r"(?:[1-9][0-9]?|100)%", value):
        return
    size = re.fullmatch(r"([1-9][0-9]*)(KB|MB|GB)?", value, re.IGNORECASE)
    require(size is not None, "Invalid filesystem quota: use 1-100%, bytes, KB, MB or GB")
    factor = {None: 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[size.group(2).upper() if size.group(2) else None]
    require(int(size.group(1)) <= (2 ** 63 - 1) // factor, "Filesystem quota exceeds the supported byte limit")


def validate_repositories(repositories, root=None):
    require(isinstance(repositories, list) and repositories, "Repository JSON must be a nonempty array")
    ids = set()
    locations = []
    filesystems = []
    for repository in repositories:
        require(isinstance(repository, dict), "Invalid repository definition")
        name = repository.get("id")
        require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name)) and name not in ids, "Repository IDs must be valid and unique")
        ids.add(name)
        require(repository.get("visibility") in ("PUBLIC", "PRIVATE"), "Invalid repository visibility")
        for flag in ("redeployment", "preserveSnapshots"):
            require(flag not in repository or type(repository[flag]) is bool, "Repository switches must be boolean")
        require(isinstance(repository.get("proxied", []), list), "Repository proxy definitions must be an array")
        provider = repository.get("storageProvider")
        require(isinstance(provider, dict), "Missing repository storage provider")
        if provider.get("type") == "fs":
            validate_quota(provider.get("quota", "100%"))
            mount = text(provider.get("mount", ""), "Filesystem mount", empty=True)
            location = filesystem_location(name, mount, root)
            for previous in filesystems:
                require(location != previous and not location.startswith(previous + "/") and not previous.startswith(location + "/"), "Filesystem repository directories must not overlap")
            filesystems.append(location)
        elif provider.get("type") == "s3":
            settings = {key: provider[key] for key in ("endpoint", "bucketName", "region", "accessKey", "secretKey", "prefix", "signer") if key in provider}
            validate({"storage": {"type": "s3", **settings}})
            require(type(provider.get("sharedBucket", False)) is bool, "S3 sharedBucket must be boolean")
            prefix = provider.get("prefix", "").strip().strip("/")
            if provider.get("sharedBucket", False):
                prefix = "/".join(part for part in (prefix, name) if part)
            namespace = prefix + "/" if prefix else ""
            location = (provider.get("endpoint", "").rstrip("/").lower(), provider["bucketName"], namespace)
            for previous in locations:
                require(location[:2] != previous[:2] or not (location[2].startswith(previous[2]) or previous[2].startswith(location[2])), "S3 repositories must not overlap in a shared bucket")
            locations.append(location)
        else:
            require(False, "Repository storage must be fs or s3")


def shell_config(data):
    fields = data.split("\0")
    require(fields[-1] == "" and len(fields) % 2 == 1, "Invalid shell configuration payload")
    require(all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in fields[:-1:2]), "Invalid shell configuration key")
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in zip(fields[:-1:2], fields[1:-1:2]))


def database_parameters(profile):
    db = profile.get("database", {})
    parameters = dict(db.get("parameters", {}))
    if db.get("type") == "postgresql" and profile.get("trustedCertificates"):
        if not any(key.lower() in ("sslfactory", "sslrootcert") for key in parameters):
            parameters["sslfactory"] = "org.postgresql.ssl.DefaultJavaSSLFactory"
    return parameters


def pem(value, kind):
    require(isinstance(value, str) and len(value) < 100000 and
            f"-----BEGIN {kind}" in value, f"Invalid {kind} PEM")


def validate(profile):
    obj(profile, ("database", "storage", "ldap", "tls", "trustedCertificates"), "profile")
    db = obj(profile.get("database", {}), ("type", "host", "port", "name", "user", "password", "parameters"), "database")
    dialect = db.get("type", "sqlite")
    require(dialect in ("sqlite", "mariadb", "mysql", "postgresql"), "Unsupported database type")
    if dialect != "sqlite":
        host(db.get("host"))
        port(db.get("port", 5432 if dialect == "postgresql" else 3306), "database port")
        require(bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}", text(db.get("name"), "database name"))), "Invalid database name")
        text(db.get("user"), "database user")
        text(db.get("password"), "database password")
        parameters = db.get("parameters", {})
        require(isinstance(parameters, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in parameters.items()), "Database parameters must contain strings")
        require(not {k.lower() for k in parameters} & {"user", "username", "password", "credentialtype", "userkey", "pwdkey"}, "Set database credentials in user/password")
        require(all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", k) and not any(ord(c) < 32 for c in v) for k, v in parameters.items()), "Invalid database parameter")
        if dialect == "mariadb":
            require(all("&" not in v for v in parameters.values()), "MariaDB parameter values cannot contain ampersands")
    else:
        require(not set(db) - {"type"}, "SQLite profile accepts only type; database is reposilite.db")

    storage = obj(profile.get("storage", {}), ("type", "root", "endpoint", "bucketName", "region", "accessKey", "secretKey", "prefix", "signer", "pathStyle", "skipBucketCreation"), "storage")
    kind = storage.get("type", "fs")
    require(kind in ("fs", "s3"), "Unsupported storage type")
    if kind == "s3":
        require(not storage.get("root"), "S3 does not use a filesystem root")
        text(storage.get("bucketName"), "S3 bucket")
        require(bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", storage["bucketName"])), "Invalid S3 bucket name")
        text(storage.get("endpoint", ""), "S3 endpoint", empty=True)
        if storage.get("endpoint"):
            url(storage["endpoint"])
        for key in ("region", "accessKey", "secretKey"):
            text(storage.get(key), f"S3 {key}")
        prefix = text(storage.get("prefix", "reposilite"), "S3 prefix", empty=True)
        require(all(p not in (".", "..") for p in prefix.strip().strip("/").split("/")), "Invalid S3 prefix")
        require(storage.get("signer", "DEFAULT") in ("DEFAULT", "LEGACY_V4"), "Invalid S3 signer")
        for key in ("pathStyle", "skipBucketCreation"):
            require(type(storage.get(key, False)) is bool, f"{key} must be boolean")
    elif "root" in storage:
        text(storage["root"], "Filesystem root", empty=True)
        root = storage["root"]
        require(not root or (bool(re.fullmatch(r"/(srv|mnt)/[A-Za-z0-9_/-]+", root)) and
                "//" not in root), "Storage root must be below /srv or /mnt")
    if kind == "fs":
        require(not set(storage) - {"type", "root"}, "Filesystem storage accepts type/root only")

    ldap = obj(profile.get("ldap", {}), ("enabled", "ssl", "hostname", "port", "baseDn", "searchUserDn", "searchUserPassword", "typeAttribute", "userAttribute", "userFilter", "userType"), "LDAP")
    require(type(ldap.get("enabled", False)) is bool, "LDAP enabled must be boolean")
    if ldap.get("enabled"):
        host(ldap.get("hostname"))
        require(type(ldap.get("ssl", True)) is bool, "LDAP ssl must be boolean")
        port(ldap.get("port", 636 if ldap.get("ssl", True) else 389), "LDAP port")
        for key in ("baseDn", "searchUserDn", "searchUserPassword"):
            text(ldap.get(key), f"LDAP {key}")
        for key, default in (("typeAttribute", "person"), ("userAttribute", "uid")):
            require(bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9;-]*", text(ldap.get(key, default), key))), "Invalid LDAP attribute")
        ldap_filter(ldap.get("userFilter", ""))
        require(ldap.get("userType", "TEMPORARY") in ("TEMPORARY", "PERSISTENT"), "Invalid LDAP userType")

    tls = obj(profile.get("tls", {}), ("mode", "hostname", "port", "certificate", "privateKey"), "TLS")
    mode = tls.get("mode", "off")
    require(mode in ("off", "self-signed", "provided"), "TLS mode must be off, self-signed or provided")
    if "hostname" in tls:
        host(tls["hostname"])
    if "port" in tls:
        port(tls["port"], "TLS port")
    if mode != "off":
        host(tls.get("hostname"))
        port(tls.get("port", 443), "TLS port")
        require(tls.get("port", 443) != int(os.environ.get("var_port", "8080")), "HTTP and TLS ports must differ")
        if mode == "provided":
            pem(tls.get("certificate"), "CERTIFICATE")
            require(isinstance(tls.get("privateKey"), str) and any(f"-----BEGIN {k}PRIVATE KEY-----" in tls["privateKey"] for k in ("", "RSA ", "EC ")), "Unencrypted PEM private key required")
    certs = profile.get("trustedCertificates", [])
    require(isinstance(certs, list) and len(certs) <= 20, "Invalid trustedCertificates")
    for cert in certs:
        pem(cert, "CERTIFICATE")
    return profile


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(content)
    path.chmod(mode)


def render(profile, root):
    """Called before the first application start, after input validation."""
    validate(profile)
    repositories = os.environ.get("var_repositories_json", "")
    if repositories:
        validate_repositories(json.loads(repositories), root)
    elif profile.get("storage", {}).get("type", "fs") == "fs":
        generated = []
        storage_root = profile.get("storage", {}).get("root", "")
        for visibility, names in (("PUBLIC", os.environ.get("var_public_repositories", "releases,snapshots")),
                                  ("PRIVATE", os.environ.get("var_private_repositories", "private"))):
            if names == "-":
                continue
            generated.extend({"id": name.strip(), "visibility": visibility, "storageProvider": {
                "type": "fs", "mount": posixpath.join(storage_root, name.strip()) if storage_root else ""
            }} for name in names.split(","))
        validate_repositories(generated, root)
    etc = root / "etc/reposilite"
    etc.mkdir(parents=True, exist_ok=True, mode=0o700)
    etc.chmod(0o700)
    db = profile.get("database", {})
    dialect = db.get("type", "sqlite")
    database = "sqlite reposilite.db"
    if dialect != "sqlite":
        if dialect == "mariadb":
            # MariaDB Connector/J does not URL-decode query parameters. Its
            # built-in ENV credential provider preserves arbitrary passwords.
            parameters = {**database_parameters(profile), "credentialType": "ENV"}
            query = "&".join(k + "=" + v for k, v in parameters.items())
            write(etc / "database.env", "export MARIADB_USER=" + shlex.quote(db["user"]) + "\nexport MARIADB_PWD=" + shlex.quote(db["password"]) + "\n")
        else:
            parameters = {**database_parameters(profile), "user": db["user"], "password": db["password"]}
            query = urlencode(parameters, quote_via=quote)
            write(etc / "database.env", "")
        database = f"jdbc:{dialect}://{db['host']}:{db.get('port', 5432 if dialect == 'postgresql' else 3306)}/{db['name']}?{query}"
        drivers = {"mariadb": "org.mariadb.jdbc.Driver", "mysql": "com.mysql.cj.jdbc.Driver", "postgresql": "org.postgresql.Driver"}
        write(etc / "database-check.json", json.dumps({"url": database, "driver": drivers[dialect]}))
    else:
        write(etc / "database.env", "")
    tls = profile.get("tls", {})
    tls_on = tls.get("mode", "off") != "off"
    config = f"database: {json.dumps(database)}\n"
    if tls_on:
        folder = etc / "tls"
        folder.mkdir(mode=0o700, exist_ok=True)
        certificate, key = folder / "certificate.pem", folder / "private-key.pem"
        if tls["mode"] == "provided":
            write(certificate, tls["certificate"])
            write(key, tls["privateKey"])
        else:
            hostname = tls["hostname"]
            try:
                ipaddress.ip_address(hostname)
                san = "IP:" + hostname
            except ValueError:
                san = "DNS:" + hostname
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-nodes", "-days", "365", "-subj", "/CN=" + hostname, "-addext", "subjectAltName=" + san, "-keyout", str(key), "-out", str(certificate)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            certificate.chmod(0o600)
        # Check key pair and hostname before starting the application.
        cert_public = subprocess.check_output(["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"])
        key_public = subprocess.check_output(["openssl", "pkey", "-in", str(key), "-pubout"], stderr=subprocess.DEVNULL)
        require(cert_public == key_public, "TLS certificate and key do not match")
        try:
            ipaddress.ip_address(tls["hostname"])
            check = "-checkip"
        except ValueError:
            check = "-checkhost"
        match = subprocess.run(["openssl", "x509", "-in", str(certificate), "-noout", check, tls["hostname"]], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        require(match.returncode == 0 and b"does match certificate" in match.stdout, "TLS certificate does not match hostname")
        subprocess.run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config += f'sslEnabled: true\nsslPort: {tls.get("port", 443)}\nkeyPath: /etc/reposilite/tls/certificate.pem /etc/reposilite/tls/private-key.pem\nenforceSsl: true\n'
    write(etc / "configuration.cdn", config)

    storage = profile.get("storage", {})
    if storage.get("type") == "s3":
        provider = {k: storage.get(k, default) for k, default in (("bucketName", ""), ("endpoint", ""), ("region", ""), ("accessKey", ""), ("secretKey", ""), ("prefix", "reposilite"), ("signer", "DEFAULT"))}
        provider.update(type="s3", sharedBucket=True)
    else:
        provider = {"type": "fs", "quota": os.environ.get("var_repository_quota", "100") + "%", "mount": ""}
    write(etc / "storage.json", json.dumps({"provider": provider, "root": storage.get("root", "")}))
    if storage.get("root"):
        # Never silently write to the LXC root disk if a host bind mount is absent.
        subprocess.run(["mountpoint", "-q", storage["root"]], check=True)
        require(os.access(storage["root"], os.W_OK), "External mount is not writable")

    ldap = profile.get("ldap", {})
    if ldap.get("enabled"):
        ldap = {"ssl": True, "typeAttribute": "person", "userAttribute": "uid", "userFilter": "", "userType": "TEMPORARY", **ldap}
        ldap.setdefault("port", 636 if ldap["ssl"] else 389)
        manager = os.environ.get("var_admin_user", "admin")
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", manager)), "Invalid local manager name")
        ldap["userFilter"] = f"(&{ldap['userFilter']}(!({ldap['userAttribute']}={manager})))"
    write(etc / "ldap.json", json.dumps(ldap))
    opts = []
    if storage.get("type") == "s3":
        if storage.get("pathStyle", False):
            opts.append("-Dreposilite.s3.pathStyleAccessEnabled=true")
        if storage.get("skipBucketCreation", False):
            opts.append("-Dreposilite.s3.skip-bucket-creation=true")
    certs = profile.get("trustedCertificates", [])
    if certs:
        import shutil
        java = Path(os.path.realpath(shutil.which("java"))).parent.parent
        store = etc / "truststore.p12"
        shutil.copyfile(java / "lib/security/cacerts", store)
        store.chmod(0o600)
        for i, cert in enumerate(certs):
            ca = etc / f"ca-{i}.pem"
            write(ca, cert)
            subprocess.run(["keytool", "-importcert", "-noprompt", "-alias", f"reposilite-custom-{i}", "-file", str(ca), "-keystore", str(store), "-storepass", "changeit"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opts += ["-Djavax.net.ssl.trustStore=/etc/reposilite/truststore.p12", "-Djavax.net.ssl.trustStorePassword=changeit"]
    values = {"tls_enabled": "yes" if tls_on else "no", "tls_host": tls.get("hostname", ""), "tls_port": str(tls.get("port", 443)), "database_type": dialect, "storage_root": storage.get("root", ""), "integration_java_opts": " ".join(opts)}
    write(etc / "runtime.conf", "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()))


def interactive(profile):
    """Read prompts from the TTY; stdout is the private JSON transport."""
    with open("/dev/tty", "r") as tty_input, open("/dev/tty", "w") as tty:
        def ask(label, default="", secret=False):
            tty.write(label + (f" [{default}]" if default and not secret else "") + ": ")
            tty.flush()
            return (getpass.getpass("", stream=tty) if secret else tty_input.readline().strip()) or default
        dialect = ask("Database: sqlite / mariadb / mysql / postgresql", "sqlite")
        db = {"type": dialect}
        if dialect != "sqlite":
            db.update(host=ask("Database host"), port=int(ask("Database port", "5432" if dialect == "postgresql" else "3306")), name=ask("Database name", "reposilite"), user=ask("Database user", "reposilite"), password=ask("Database password", secret=True))
        profile["database"] = db
        kind = ask("Artifact storage: fs / s3", "fs")
        storage = {"type": kind}
        if kind == "s3":
            for key, label, default in (("endpoint", "S3 endpoint (empty for AWS)", ""), ("bucketName", "S3 bucket", ""), ("region", "S3 region", "us-east-1"), ("accessKey", "S3 access key", ""), ("secretKey", "S3 secret key", ""), ("prefix", "S3 prefix", "reposilite"), ("signer", "S3 signer: DEFAULT / LEGACY_V4 (R2)", "DEFAULT")):
                storage[key] = ask(label, default, key in ("accessKey", "secretKey"))
            storage["pathStyle"] = ask("S3 path-style access (MinIO): yes / no", "no") == "yes"
            storage["skipBucketCreation"] = ask("Use existing bucket only: yes / no", "yes") == "yes"
        else:
            storage["root"] = ask("Existing LXC mount root under /srv or /mnt (empty for local disk)")
        profile["storage"] = storage
        if ask("Enable LDAP: yes / no", "no") == "yes":
            ssl = ask("Use LDAPS: yes / no", "yes") == "yes"
            ldap = {"enabled": True, "ssl": ssl, "hostname": ask("LDAP host"), "port": int(ask("LDAP port", "636" if ssl else "389"))}
            for key, label, default in (("baseDn", "Base DN", ""), ("searchUserDn", "Search user DN", ""), ("searchUserPassword", "Search user password", ""), ("typeAttribute", "Object class", "person"), ("userAttribute", "Username attribute", "uid"), ("userFilter", "Additional LDAP filter (empty for none)", ""), ("userType", "Mapped token: TEMPORARY / PERSISTENT", "TEMPORARY")):
                ldap[key] = ask(label, default, key == "searchUserPassword")
            profile["ldap"] = ldap
        mode = ask("Native TLS: off / self-signed / provided", "off")
        tls = {"mode": mode}
        if mode != "off":
            tls.update(hostname=ask("TLS DNS hostname or IPv4"), port=int(ask("HTTPS port", "443")))
            if mode == "provided":
                tls["certificate"] = Path(ask("Fullchain PEM file on Proxmox host")).read_text()
                tls["privateKey"] = Path(ask("Unencrypted private key PEM file on Proxmox host")).read_text()
        profile["tls"] = tls
        ca_files = ask("Trusted CA PEM files on Proxmox (comma separated, empty for system trust)")
        if ca_files:
            profile["trustedCertificates"] = [Path(p.strip()).read_text() for p in ca_files.split(",")]
    return validate(profile)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "interactive", "render", "shell-config"))
    parser.add_argument("--file")
    parser.add_argument("--root", default="/")
    args = parser.parse_args()
    try:
        if args.action == "shell-config":
            print(shell_config(sys.stdin.read()), end="")
            return 0
        if args.file:
            path = Path(args.file)
            require(path.stat().st_mode & 0o077 == 0, "Profile file must have mode 600 or stricter")
            profile = json.loads(path.read_text())
        else:
            profile = json.loads(os.environ.get("var_integrations_json", "{}"))
        if args.action == "interactive":
            profile = interactive(profile)
        validate(profile)
        repositories = os.environ.get("var_repositories_json", "")
        if repositories:
            validate_repositories(json.loads(repositories))
        if args.action == "render":
            render(profile, Path(args.root))
        else:
            print(json.dumps(profile, separators=(",", ":")))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Do not echo supplied values or subprocess output containing secrets.
        print("Reposilite profile error: " + (str(error) if isinstance(error, ProfileError) else "file, certificate or configuration operation failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
