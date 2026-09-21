from homeassistant.const import Platform

DOMAIN = "dahua_arc"
CONF_HTTP_PORT = "http_port"
CONF_DHIP_PORT = "dhip_port"
CONF_PERIODIC_RESYNC = "periodic_resync_seconds"
CONF_AUTO_AREA_MATCH = "auto_area_match"
CONF_AREA_MATCH_THRESHOLD = "area_match_threshold"
CONF_AREA_MATCH_AREAS = "area_match_areas"
CONF_ZONE_AREA_DECISIONS = "zone_area_decisions"
CONF_ZONE_AREA_OWNERSHIP = "zone_area_ownership"
CONF_ARC_SERIAL = "arc_serial"
CONF_ENABLE_RESEARCH_FEATURES = "enable_research_features"

DEFAULT_HTTP_PORT = 80
DEFAULT_DHIP_PORT = 5000
DEFAULT_PERIODIC_RESYNC = 300
DEFAULT_AUTO_AREA_MATCH = False
DEFAULT_ENABLE_RESEARCH_FEATURES = False
DEFAULT_AREA_MATCH_THRESHOLD = 90

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
]

ISSUE_RESEARCH_ENABLED = "research_enabled"
ISSUE_NO_PRIMARY_ZONES = "no_primary_zones"
ISSUE_INVENTORY_ERROR = "inventory_error"
