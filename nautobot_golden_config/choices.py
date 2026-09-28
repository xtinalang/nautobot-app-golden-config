"""Choicesets for golden config."""

from nautobot.apps.choices import ChoiceSet


class ComplianceRuleConfigTypeChoice(ChoiceSet):
    """Choiceset used by ComplianceRule."""

    TYPE_CLI = "cli"
    TYPE_JSON = "json"
    TYPE_XML = "xml"

    CHOICES = (
        (TYPE_CLI, "CLI"),
        (TYPE_JSON, "JSON"),
        (TYPE_XML, "XML"),
    )


class RemediationTypeChoice(ChoiceSet):
    """Choiceset used by RemediationSetting."""

    TYPE_HIERCONFIG = "hierconfig"
    TYPE_CUSTOM = "custom_remediation"

    CHOICES = (
        (TYPE_HIERCONFIG, "HIERCONFIG"),
        (TYPE_CUSTOM, "CUSTOM_REMEDIATION"),
    )


class BackupDiffWindowChoice(ChoiceSet):
    """Relative history windows offered by the Backup History Diff jobs.

    Shared by "Sync Backup Version Table" (how far back to read Git) and "Clean Up Backup Version Table"
    (how far back to keep), so the two jobs cannot drift apart and a window means the same thing in both.
    Values are a number of days as a string, because a Job ``ChoiceVar`` submits strings; the empty value
    means "no window".
    """

    WINDOW_ALL = ""
    WINDOW_30 = "30"
    WINDOW_60 = "60"
    WINDOW_90 = "90"
    WINDOW_180 = "180"
    WINDOW_365 = "365"

    CHOICES = (
        (WINDOW_ALL, "All history"),
        (WINDOW_30, "Last 30 days"),
        (WINDOW_60, "Last 60 days"),
        (WINDOW_90, "Last 90 days"),
        (WINDOW_180, "Last 180 days"),
        (WINDOW_365, "Last 365 days"),
    )


class ConfigPlanTypeChoice(ChoiceSet):
    """Choiceset used by ConfigPlan."""

    TYPE_INTENDED = "intended"
    TYPE_MISSING = "missing"
    TYPE_REMEDIATION = "remediation"
    TYPE_MANUAL = "manual"

    CHOICES = (
        (TYPE_INTENDED, "Intended"),
        (TYPE_MISSING, "Missing"),
        (TYPE_REMEDIATION, "Remediation"),
        (TYPE_MANUAL, "Manual"),
    )
