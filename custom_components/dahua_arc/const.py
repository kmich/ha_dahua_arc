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
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
]

ISSUE_RESEARCH_ENABLED = "research_enabled"
ISSUE_NO_PRIMARY_ZONES = "no_primary_zones"
ISSUE_INVENTORY_ERROR = "inventory_error"
ISSUE_SERIAL_MISMATCH = "serial_mismatch"
CONF_REMATCH_EXISTING = "rematch_existing"

# Opt-in arm/disarm control (options only; see docs/arm-control/03).
CONF_ENABLE_ARM_CONTROL = "enable_arm_control"
CONF_ARM_CODE_HASH = "arm_code_hash"
CONF_CODE_DISARM_REQUIRED = "code_disarm_required"
CONF_CODE_ARM_REQUIRED = "code_arm_required"
CONF_ARM_MODES = "arm_modes"
CONF_ARM_CONTROL_ACK = "arm_control_acknowledged"
CONF_ARM_CODE_ACK_NO_CODE = "arm_code_ack_no_code"
# Form-only fields that never reach the stored options.
CONF_ARM_CODE = "arm_code"
CONF_CLEAR_ARM_CODE = "clear_arm_code"
CONF_ACKNOWLEDGE_CONTROL = "acknowledge_control"

DEFAULT_ENABLE_ARM_CONTROL = False
DEFAULT_CODE_DISARM_REQUIRED = True
DEFAULT_CODE_ARM_REQUIRED = False
ARM_MODE_HOME = "armed_home"
ARM_MODE_AWAY = "armed_away"
ARM_MODE_NIGHT = "armed_night"
# Night (p2) is offered only once it is verified on hardware.
VERIFIED_ARM_MODES = (ARM_MODE_HOME, ARM_MODE_AWAY)
DEFAULT_ARM_MODES = list(VERIFIED_ARM_MODES)

ISSUE_ARM_CONTROL_WITHOUT_CODE = "arm_control_without_code"
ISSUE_ARM_CONTROL_UNSUPPORTED = "arm_control_unsupported"
