"""Source trust classification: which origins may be treated as engineering ground truth.

There is no human approval step (Absolute-Trust RAG). Instead, trust is a deterministic
function of the origin: manufacturer domains and official framework repositories are
authoritative; everything else is recorded but flagged UNOFFICIAL and surfaced by the verifier.
"""

from enum import Enum
from urllib.parse import urlparse


class TrustTier(str, Enum):
    OFFICIAL_MANUFACTURER = "OFFICIAL_MANUFACTURER"
    OFFICIAL_REPO = "OFFICIAL_REPO"
    USER = "USER"
    UNOFFICIAL = "UNOFFICIAL"


OFFICIAL_DOMAINS = {
    # MCU / SoC vendors
    "st.com", "ti.com", "microchip.com", "atmel.com", "nxp.com", "nordicsemi.com",
    "espressif.com", "raspberrypi.com", "renesas.com", "silabs.com", "infineon.com",
    "analog.com", "maximintegrated.com", "gigadevice.com", "wch-ic.com", "cypress.com",
    "arm.com", "onsemi.com", "lattice.com", "nuvoton.com", "ambiq.com",
    "arterytek.com", "arterychip.com",
    # Sensor / peripheral / driver vendors
    "invensense.tdk.com", "tdk.com", "bosch-sensortec.com", "sensirion.com",
    "melexis.com", "ams-osram.com", "ams.com", "vishay.com", "rohm.com",
    "memsic.com", "honeywell.com", "murata.com", "fortiortech.com", "fd-ic.com",
    "semtech.com", "cirrus.com", "everlight.com", "winbond.com", "macronix.com",
    "knowles.com", "ublox.com", "u-blox.com", "quectel.com", "sitime.com",
}

# Hosts that are official for specific vendors/frameworks but aren't their own domain.
OFFICIAL_HOST_SUFFIXES = {
    "docs.espressif.com", "docs.zephyrproject.org", "docs.platformio.org",
    "arduino.cc", "docs.arduino.cc", "ardupilot.org", "betaflight.com",
    "docs.px4.io", "px4.io", "micropython.org", "developer.arm.com",
    "datasheets.raspberrypi.com", "docs.nordicsemi.com", "infocenter.nordicsemi.com",
}

OFFICIAL_GITHUB_ORGS = {
    "stmicroelectronics", "espressif", "raspberrypi", "nrfconnect", "nordicsemiconductor",
    "ardupilot", "betaflight", "px4", "zephyrproject-rtos", "platformio", "arduino",
    "microchip-pic-avr-solutions", "analogdevicesinc", "nxp-mcuxpresso", "tdk-invensense",
    "boschsensortec", "sensirion", "infineon", "renesas", "ti-sitara", "arm-software",
    "armmbed", "micropython", "arterytek", "am32", "bitdump", "simplefoc", "madflight",
}


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().split(":")[0]


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify_url(url: str) -> TrustTier:
    """Deterministically classify a source URL into a trust tier."""
    if not url:
        return TrustTier.UNOFFICIAL
    if url.startswith("user://"):
        return TrustTier.USER
    host = _host(url)
    if any(_matches(host, d) for d in OFFICIAL_DOMAINS | OFFICIAL_HOST_SUFFIXES):
        return TrustTier.OFFICIAL_MANUFACTURER
    if host in ("github.com", "raw.githubusercontent.com", "www.github.com", "gitlab.com"):
        parts = [p for p in urlparse(url).path.split("/") if p]
        if parts and parts[0].lower() in OFFICIAL_GITHUB_ORGS:
            return TrustTier.OFFICIAL_REPO
    return TrustTier.UNOFFICIAL


def is_authoritative(tier: TrustTier) -> bool:
    return tier in (TrustTier.OFFICIAL_MANUFACTURER, TrustTier.OFFICIAL_REPO, TrustTier.USER)
