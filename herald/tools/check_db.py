"""Check the database connection and report what is actually deployed.

    python tools/check_db.py

There are two different passwords in play and the errors Oracle returns for
them are not obviously distinguishable, which is the reason this exists rather
than just reading a traceback:

    ORACLE_WALLET_PASSWORD   typed when the wallet was downloaded from OCI
    ORACLE_PASSWORD          the ADMIN password for the database

A wrong wallet password fails while loading the encrypted key, before the
database is contacted. A wrong database password is ORA-01017, which means the
wallet, the route and TLS all worked. A refused listener is neither, and means
the database is not accepting connections at all, which is usually because it
is stopped or resuming rather than because anything is misconfigured.

Nothing here writes. Safe to run against production.
"""
from __future__ import annotations

import pathlib
import re
import sys
import time

# A stopped Autonomous Database refuses the connection that wakes it, so a
# single refusal is not evidence of anything.
ATTEMPTS = 3
RETRY_WAIT = 8

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def no_retry_dsn(wallet: pathlib.Path | None, alias: str) -> str:
    """The full descriptor for `alias` with its retries removed, or the alias.

    The wallet ships retry_count=20 and retry_delay=3, so an unanswered
    connection takes three and a half minutes to report. That is correct for a
    running service riding out a blip, and useless to someone waiting at a
    terminal for a yes or no. Passing retry_count as a parameter does not help:
    the value inside the descriptor wins, so the descriptor is what has to
    change.
    """
    if not wallet:
        return alias
    tns = wallet / "tnsnames.ora"
    if not tns.exists():
        return alias
    # Entries can wrap onto indented continuation lines, so those are joined
    # first. Only spaces and tabs: \s would swallow the blank lines that
    # separate one entry from the next and weld the whole file into one.
    text = re.sub(r"\n[ \t]+", " ", tns.read_text(errors="replace"))
    for line in text.splitlines():
        name, _, descriptor = line.partition("=")
        if name.strip().lower() == alias.lower() and descriptor.strip():
            out = re.sub(r"retry_count\s*=\s*\d+", "retry_count=0", descriptor,
                         flags=re.I)
            return re.sub(r"retry_delay\s*=\s*\d+", "retry_delay=0", out, flags=re.I)
    return alias


def diagnose(exc: Exception, dsn: str) -> None:
    text = str(exc)
    print(f"  {type(exc).__name__}: {text.strip()}")
    # oracledb wraps the real reason; DPY-6005 says only "cannot connect" while
    # the cause underneath names the actual failure.
    cause = exc.__cause__ or exc.__context__
    while cause is not None:
        print(f"  caused by {type(cause).__name__}: {str(cause).strip()}")
        text += " " + str(cause)
        cause = cause.__cause__ or cause.__context__

    print()
    if "ORA-01017" in text:
        print("  The wallet, the route and TLS all worked and the database rejected\n"
              "  the credentials. ORACLE_PASSWORD is wrong.")
    elif "DPY-6000" in text or "ORA-12506" in text or "ORA-12514" in text:
        print(f"  Oracle's listener answered and then refused, {ATTEMPTS} times running,\n"
              "  so the route is open and this is not a firewall. In order of how\n"
              "  often it turns out to be each:\n"
              "    - the database is STOPPED or resuming. An Autonomous Database\n"
              "      refuses the connection that wakes it, and can refuse for a\n"
              "      minute or so afterwards while it comes up. ORDS on 443 keeps\n"
              "      answering throughout, so a working browser proves nothing.\n"
              "      Retrying is listed first because it is the one cause here that\n"
              "      resolves itself: try again in a minute before changing anything.\n"
              "    - ORACLE_WALLET_PASSWORD is wrong, so the client certificate in\n"
              "      ewallet.pem could not be decrypted and none was presented. This\n"
              "      database requires mutual TLS, and the listener refuses rather\n"
              "      than reporting a certificate problem, so this looks nothing\n"
              "      like a password error. Note the wallet password is often set to\n"
              "      the same string as the database password.\n"
              "    - an access control list limits which addresses may connect and\n"
              "      this one is not on it.\n"
              "    - the database was cloned or recreated, so the wallet is stale and\n"
              "      a fresh one needs downloading.")
    elif ("DPY-4029" in text or "keystore" in text.lower() or "PKCS" in text
          or "decrypt" in text.lower() or "password" in text.lower()):
        print("  Failed before reaching the database. ORACLE_WALLET_PASSWORD is wrong\n"
              "  or unset. That is the password typed when downloading the wallet,\n"
              "  not the database password.")
    elif "DPY-6005" in text or "timeout" in text.lower():
        print("  Never reached the host. Check outbound access to\n"
              "  adb.ap-mumbai-1.oraclecloud.com on port 1522.")
    elif "DPY-4000" in text:
        print(f"  The alias {dsn!r} was not found in tnsnames.ora.")


def report(cur, schema: str) -> int:
    cur.execute("SELECT user, SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual")
    login, current = cur.fetchone()
    print(f"  connected as {login}, current schema {current}")

    cur.execute("""SELECT table_name FROM all_tables
                   WHERE owner = :o AND table_name LIKE 'HARALD%'
                   ORDER BY table_name""", {"o": schema.upper()})
    tables = [r[0] for r in cur.fetchall()]
    if not tables:
        print(f"\n  No HARALD tables in {schema}. Run schema/harald_schema.sql in\n"
              f"  Database Actions, then run this again.")
        return 1

    print(f"\n  {len(tables)} tables in {schema}:")
    for name in tables:
        cur.execute(f"SELECT COUNT(*) FROM {schema}.{name}")
        print(f"    {name.lower():<30} {cur.fetchone()[0]:>7,} rows")

    cur.execute("""SELECT trigger_name, status FROM all_triggers
                   WHERE owner = :o AND trigger_name LIKE 'HARALD%'
                   ORDER BY trigger_name""", {"o": schema.upper()})
    triggers = cur.fetchall()
    print("\n  retention triggers: " + (
        ", ".join(f"{n.lower()} ({s.lower()})" for n, s in triggers)
        if triggers else "NONE - approved proposals are not protected"))
    return 0


def main() -> int:
    try:
        from app.config import ConfigError, cfg
    except Exception as exc:  # noqa: BLE001
        print(f"configuration failed to load: {exc}")
        return 2

    print("configuration")
    print(f"  user            {cfg.oracle_user}")
    print(f"  dsn             {cfg.oracle_dsn or '(not set)'}")
    print(f"  schema          {cfg.app_schema}")
    print(f"  TNS_ADMIN       {cfg.tns_admin or '(not set)'}")
    print(f"  db password     {'set' if cfg.oracle_password else 'NOT SET'}")
    print(f"  wallet password {'set' if cfg.wallet_password else 'NOT SET'}")

    wallet = pathlib.Path(cfg.tns_admin) if cfg.tns_admin else None
    if wallet and wallet.is_dir():
        present = {p.name for p in wallet.iterdir()}
        missing = {"tnsnames.ora", "ewallet.pem", "cwallet.sso"} - present
        print(f"  wallet files    {len(present)} present"
              + (f", MISSING {sorted(missing)}" if missing else ""))
        tns = wallet / "tnsnames.ora"
        if tns.exists():
            aliases = re.findall(r"^(\w+)\s*=", tns.read_text(errors="replace"), re.M)
            print(f"  aliases         {', '.join(aliases)}")
            if cfg.oracle_dsn and cfg.oracle_dsn not in aliases:
                print(f"\n  ORACLE_DSN {cfg.oracle_dsn!r} is not one of those aliases.")
                return 2
    elif cfg.tns_admin:
        print(f"\n  TNS_ADMIN points at {cfg.tns_admin!r}, which is not a directory.")
        return 2

    try:
        cfg.validate()
    except ConfigError as exc:
        print(f"\n{exc}")
        return 2

    import oracledb

    print("\nconnecting...")
    connection = None
    # Retries are stripped from the descriptor so a dead host answers in seconds
    # rather than three and a half minutes, but that also removed the cushion
    # that hid a waking database. A stopped Autonomous Database refuses the
    # connection that resumes it, so one refusal means nothing; a few in a row
    # mean something. This retries deliberately and briefly, and says so, rather
    # than reporting a certificate failure that was never the problem.
    for attempt in range(1, ATTEMPTS + 1):
        try:
            connection = oracledb.connect(
                user=cfg.oracle_user,
                password=cfg.oracle_password,
                dsn=no_retry_dsn(wallet, cfg.oracle_dsn),
                config_dir=cfg.tns_admin or None,
                wallet_location=cfg.tns_admin or None,
                wallet_password=cfg.wallet_password or None,
                tcp_connect_timeout=10,
            )
            if attempt > 1:
                print(f"  connected on attempt {attempt}. The earlier refusals were "
                      "the database resuming.")
            break
        except Exception as exc:  # noqa: BLE001
            # Only a refusal is worth retrying. A rejected password will be
            # rejected just as firmly the second time.
            text = str(exc) + " " + str(exc.__cause__ or "")
            transient = "DPY-6000" in text or "ORA-12506" in text
            if not transient or attempt == ATTEMPTS:
                diagnose(exc, cfg.oracle_dsn)
                return 1
            print(f"  attempt {attempt} refused; the database may be resuming, "
                  f"retrying in {RETRY_WAIT}s...")
            time.sleep(RETRY_WAIT)

    try:
        cur = connection.cursor()
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {cfg.app_schema}")
        code = report(cur, cfg.app_schema)
    finally:
        connection.close()

    if code == 0:
        print("\nOK")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
