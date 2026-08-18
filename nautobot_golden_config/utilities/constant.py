"""Storage of data that will not change throughout the life cycle of application."""

from django.conf import settings
from django.utils.module_loading import import_string

PLUGIN_CFG = settings.PLUGINS_CONFIG["nautobot_golden_config"]

ENABLE_INTENDED = PLUGIN_CFG["enable_intended"]
ENABLE_COMPLIANCE = PLUGIN_CFG["enable_compliance"]
ENABLE_BACKUP = PLUGIN_CFG["enable_backup"]
ENABLE_SOTAGG = PLUGIN_CFG["enable_sotagg"]
ENABLE_PLAN = PLUGIN_CFG["enable_plan"]
ENABLE_DEPLOY = PLUGIN_CFG["enable_deploy"]
ENABLE_POSTPROCESSING = PLUGIN_CFG["enable_postprocessing"]
ENABLE_BACKUP_DIFF_INDEX = PLUGIN_CFG["enable_backup_diff_index"]
# Device ceiling for the git-native fleet list on the Backup History Diff landing page. That list walks
# every in-scope device, so its cost is linear in fleet size: ~0.9s at 10K devices, ~9.6s at 100K. Above
# this the page skips the walk and points at `enable_backup_diff_index` instead of hanging. Raise it if
# you would rather wait; per-device diffs are unaffected either way.
BACKUP_DIFF_MAX_FALLBACK_FLEET = PLUGIN_CFG["backup_diff_max_fallback_fleet"]
DEFAULT_DEPLOY_STATUS = PLUGIN_CFG["default_deploy_status"]

CONFIG_FEATURES = {
    "intended": ENABLE_INTENDED,
    "compliance": ENABLE_COMPLIANCE,
    "backup": ENABLE_BACKUP,
    "sotagg": ENABLE_SOTAGG,
    "postprocessing": ENABLE_POSTPROCESSING,
}

JINJA_ENV = PLUGIN_CFG["jinja_env"]
if not JINJA_ENV.get("undefined"):
    raise ValueError("The `jinja_env` setting did not include the required key for `undefined`.")
if isinstance(JINJA_ENV["undefined"], str):
    JINJA_ENV["undefined"] = import_string(JINJA_ENV["undefined"])
