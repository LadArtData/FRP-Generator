"""Configuration. Values come from the environment; required values are
validated at startup so the container fails fast and loudly rather than
half-working."""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field


def _load_dotenv() -> None:
    """Read .env beside the project root, if there is one.

    A real environment always wins: in a container these come from the task
    definition and the file will not exist. This is for running the tools by
    hand, where otherwise every command needs the whole environment retyped and
    the one that gets forgotten fails somewhere unhelpful, such as the driver
    reporting no configuration directory rather than no wallet.

    Deliberately not python-dotenv. This is a dozen lines and one fewer pinned
    dependency in the image.
    """
    path = pathlib.Path(__file__).resolve().parent.parent / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        # Quoted values keep any trailing spaces someone meant to include.
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


_load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _region_from_ocid(ocid: str) -> str:
    """The region embedded in an OCID, or "" for the region-less forms.

    OCIDs are ocid1.<type>.<realm>.<region>.<unique>, and tenancy and
    compartment OCIDs leave the region segment empty. A model OCID does not:
    it names the region that actually serves it.
    """
    parts = (ocid or "").split(".")
    return parts[3] if len(parts) > 4 else ""


@dataclass(frozen=True)
class Config:
    # Oracle Autonomous Database
    oracle_user: str = field(default_factory=lambda: os.getenv("ORACLE_USER", "ADMIN"))
    oracle_password: str = field(default_factory=lambda: os.getenv("ORACLE_PASSWORD", ""))
    oracle_dsn: str = field(default_factory=lambda: os.getenv("ORACLE_DSN", ""))
    # The one ADB login is ADMIN, but every product table lives in the ITERIA_AI
    # schema. The pool sets CURRENT_SCHEMA to this on each connection so the app's
    # unqualified SQL resolves there.
    app_schema: str = field(default_factory=lambda: os.getenv("HARALD_APP_SCHEMA", "ITERIA_AI"))
    tns_admin: str = field(default_factory=lambda: os.getenv("TNS_ADMIN", ""))
    wallet_password: str = field(default_factory=lambda: os.getenv("ORACLE_WALLET_PASSWORD", ""))
    pool_min: int = field(default_factory=lambda: _int("ORACLE_POOL_MIN", 2))
    pool_max: int = field(default_factory=lambda: _int("ORACLE_POOL_MAX", 10))
    # Without this a blocked route to the ADB hangs for three and a half minutes
    # before reporting anything, which during a deploy reads as a wedged
    # container rather than a firewall.
    connect_timeout: int = field(default_factory=lambda: _int("ORACLE_CONNECT_TIMEOUT", 15))
    # The wallet's own descriptor carries retry_count=20 with a 3 second delay,
    # so a refused connection takes three and a half minutes to surface. That is
    # right for a running service riding out a brief blip and wrong for a
    # startup check. 0 leaves the descriptor's value alone.
    retry_count: int = field(default_factory=lambda: _int("ORACLE_RETRY_COUNT", 0))

    # OCI Generative AI. The only model service in the system. Models are named
    # by OCID, not by a friendly name, so GENAI_MODEL_OCID is what actually
    # routes; GENAI_MODEL is carried for logs and the admin screen.
    genai_region: str = field(
        default_factory=lambda: os.getenv("GENAI_REGION") or os.getenv("OCI_REGION", ""))
    genai_model_name: str = field(
        default_factory=lambda: os.getenv("GENAI_MODEL", ""))
    genai_compartment: str = field(
        default_factory=lambda: os.getenv("GENAI_COMPARTMENT_ID")
        or os.getenv("OCI_COMPARTMENT_ID", ""))
    # Draft and polish are two passes over the same endpoint. Oracle serves one
    # model per OCID, so both default to the configured model; set the polish
    # OCID separately only if a second model is provisioned.
    draft_model: str = field(
        default_factory=lambda: os.getenv("HARALD_DRAFT_MODEL_OCID")
        or os.getenv("GENAI_MODEL_OCID", ""))
    polish_model: str = field(
        default_factory=lambda: os.getenv("HARALD_POLISH_MODEL_OCID")
        or os.getenv("GENAI_MODEL_OCID", ""))
    llm_timeout: float = field(default_factory=lambda: _float("HARALD_LLM_TIMEOUT", 180.0))
    llm_max_retries: int = field(default_factory=lambda: _int("HARALD_LLM_MAX_RETRIES", 5))
    llm_concurrency: int = field(default_factory=lambda: _int("HARALD_LLM_CONCURRENCY", 4))

    @property
    def genai_endpoint(self) -> str:
        """Regional inference endpoint. Explicit override wins so a private
        endpoint or a new region can be set without a code change."""
        override = os.getenv("OCI_GENAI_ENDPOINT")
        if override:
            return override
        return f"https://inference.generativeai.{self.genai_region}.oci.oraclecloud.com"

    # Object Storage. HARALD has its own bucket for ERP proposals, separate from
    # SCOUT's resume bucket. It lives in the tenancy home region alongside the
    # database, not in the Generative AI region, so the region is tracked
    # separately rather than reusing genai_region.
    bucket_ocid: str = field(
        default_factory=lambda: os.getenv("HARALD_BUCKET_OCID", ""))
    bucket_region: str = field(
        default_factory=lambda: os.getenv("HARALD_BUCKET_REGION")
        or os.getenv("OCI_REGION", ""))
    # Buckets are listed per compartment. Usually the same compartment that
    # bills model calls, but it does not have to be.
    bucket_compartment: str = field(
        default_factory=lambda: os.getenv("HARALD_BUCKET_COMPARTMENT_ID")
        or os.getenv("GENAI_COMPARTMENT_ID")
        or os.getenv("OCI_COMPARTMENT_ID", ""))

    # Local embeddings. Runs inside the container; no external service.
    embed_model: str = field(default_factory=lambda: os.getenv("HARALD_EMBED_MODEL", "BAAI/bge-base-en-v1.5"))
    embed_dim: int = field(default_factory=lambda: _int("HARALD_EMBED_DIM", 768))

    # Retrieval
    top_k: int = field(default_factory=lambda: _int("HARALD_TOP_K", 6))
    strong_match_distance: float = field(default_factory=lambda: _float("HARALD_STRONG_MATCH", 0.35))

    # Identity. The database has one shared login, so HARALD holds its own
    # application identity (pick-a-name sign-in). Pricing/final approval are
    # gated by the approver *role* on that name, not by a second passphrase.
    session_secret: str = field(default_factory=lambda: os.getenv("HARALD_SESSION_SECRET", ""))
    session_hours: int = field(default_factory=lambda: _int("HARALD_SESSION_HOURS", 12))

    # Export
    soffice_bin: str = field(default_factory=lambda: os.getenv("HARALD_SOFFICE_BIN", "soffice"))
    log_level: str = field(default_factory=lambda: os.getenv("HARALD_LOG_LEVEL", "INFO"))

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("ORACLE_PASSWORD", self.oracle_password),
                ("ORACLE_DSN", self.oracle_dsn),
                ("GENAI_MODEL_OCID", self.draft_model),
                ("GENAI_REGION", self.genai_region),
                # Every OCI GenAI call is billed against a compartment; the SDK
                # rejects the request without one.
                ("GENAI_COMPARTMENT_ID", self.genai_compartment),
                ("HARALD_SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        for name, value in (("GENAI_MODEL_OCID", self.draft_model),
                            ("HARALD_POLISH_MODEL_OCID", self.polish_model)):
            if not value.startswith("ocid1.generativeaimodel."):
                raise ConfigError(
                    f"{name} must be a model OCID beginning 'ocid1.generativeaimodel.', "
                    f"got {value!r}. The friendly name goes in GENAI_MODEL."
                )
            # A model is served from one region, and the OCID says which. If the
            # endpoint is built for a different one the call fails with a 404 on
            # the model rather than anything naming the region, which is a long
            # afternoon. The usual cause is OCI_REGION being picked up as the
            # GenAI region when GENAI_REGION was not set.
            ocid_region = _region_from_ocid(value)
            if ocid_region and ocid_region != self.genai_region:
                raise ConfigError(
                    f"{name} is served from {ocid_region}, but the inference "
                    f"endpoint is built for {self.genai_region}. Set "
                    f"GENAI_REGION={ocid_region}, or use a model OCID from "
                    f"{self.genai_region}. Note that GENAI_REGION falls back to "
                    f"OCI_REGION, which is the tenancy home region and is often "
                    f"not where Generative AI is being called."
                )

        # The tenancy is the root compartment, so a tenancy OCID here is valid
        # and simply means everything is billed at the root. Anything else is a
        # paste error, and the SDK's own message for it names neither variable.
        if not self.genai_compartment.startswith(
                ("ocid1.compartment.", "ocid1.tenancy.")):
            raise ConfigError(
                f"GENAI_COMPARTMENT_ID must be a compartment or tenancy OCID, got "
                f"{self.genai_compartment!r}."
            )
        if self.pool_min < 1 or self.pool_max < self.pool_min:
            raise ConfigError("ORACLE_POOL_MIN must be >= 1 and <= ORACLE_POOL_MAX")
        if self.llm_concurrency < 1:
            raise ConfigError("HARALD_LLM_CONCURRENCY must be >= 1")
        if not re.match(r"^[A-Za-z][A-Za-z0-9_$#]*$", self.app_schema):
            raise ConfigError(
                f"HARALD_APP_SCHEMA must be a valid Oracle identifier, got {self.app_schema!r}")


cfg = Config()
