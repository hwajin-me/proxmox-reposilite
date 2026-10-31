#!/usr/bin/env python3
"""Validate install profiles and render native Reposilite configuration. No proxy."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import quote, urlencode, urlsplit


def require(condition, message):
    if not condition:
        raise ValueError(message)


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
        if storage.get("endpoint"):
            url(storage["endpoint"])
        for key in ("region", "accessKey", "secretKey"):
            text(storage.get(key), f"S3 {key}")
        prefix = text(storage.get("prefix", "reposilite"), "S3 prefix", empty=True)
        require(all(p not in (".", "..") for p in prefix.split("/")), "Invalid S3 prefix")
        require(storage.get("signer", "DEFAULT") in ("DEFAULT", "LEGACY_V4"), "Invalid S3 signer")
        for key in ("pathStyle", "skipBucketCreation"):
            require(type(storage.get(key, False)) is bool, f"{key} must be boolean")
    elif storage.get("root"):
        root = storage["root"]
        require(isinstance(root, str) and bool(re.fullmatch(r"/(srv|mnt)/[A-Za-z0-9_/-]+", root)) and
                "//" not in root, "Storage root must be below /srv or /mnt")
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
        text(ldap.get("userFilter", ""), "LDAP filter", empty=True)
        require(ldap.get("userType", "TEMPORARY") in ("TEMPORARY", "PERSISTENT"), "Invalid LDAP userType")

    tls = obj(profile.get("tls", {}), ("mode", "hostname", "port", "certificate", "privateKey"), "TLS")
    mode = tls.get("mode", "off")
    require(mode in ("off", "self-signed", "provided"), "TLS mode must be off, self-signed or provided")
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
            parameters = {**db.get("parameters", {}), "credentialType": "ENV"}
            query = "&".join(k + "=" + v for k, v in parameters.items())
            write(etc / "database.env", "export MARIADB_USER=" + shlex.quote(db["user"]) + "\nexport MARIADB_PWD=" + shlex.quote(db["password"]) + "\n")
        else:
            parameters = {**db.get("parameters", {}), "user": db["user"], "password": db["password"]}
            query = urlencode(parameters, quote_via=quote)
            write(etc / "database.env", "")
        database = f"jdbc:{dialect}://{db['host']}:{db.get('port', 5432 if dialect == 'postgresql' else 3306)}/{db['name']}?{query}"
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
        match = subprocess.check_output(["openssl", "x509", "-in", str(certificate), "-noout", check, tls["hostname"]], stderr=subprocess.DEVNULL)
        require(b"does match certificate" in match, "TLS certificate does not match hostname")
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
    """Use the controlling TTY so stdout contains only JSON, never credentials."""
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
    parser.add_argument("action", choices=("validate", "interactive", "render"))
    parser.add_argument("--file")
    parser.add_argument("--root", default="/")
    args = parser.parse_args()
    try:
        if args.file:
            path = Path(args.file)
            require(path.stat().st_mode & 0o077 == 0, "Profile file must have mode 600 or stricter")
            profile = json.loads(path.read_text())
        else:
            profile = json.loads(os.environ.get("var_integrations_json", "{}"))
        if args.action == "interactive":
            profile = interactive(profile)
        validate(profile)
        if args.action == "render":
            render(profile, Path(args.root))
        else:
            print(json.dumps(profile, separators=(",", ":")))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Do not echo supplied values or subprocess output containing secrets.
        print("Invalid integration profile: " + (str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "file, certificate or configuration operation failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
