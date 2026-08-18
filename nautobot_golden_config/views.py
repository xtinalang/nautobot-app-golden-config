"""Django views for Nautobot Golden Configuration."""  # pylint: disable=too-many-lines

import ipaddress
import json
import logging
import uuid
from datetime import datetime
from urllib.parse import urlencode

import yaml
from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, ExpressionWrapper, FloatField, Max, Q, Sum, Value
from django.db.models.functions import Coalesce, NullIf
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.html import format_html
from django.utils.timezone import make_aware
from django.views.generic import TemplateView, View
from django_pivot.pivot import pivot
from django_tables2 import RequestConfig
from nautobot.apps import views
from nautobot.apps.ui import (
    EChartsPanel,
    EChartsThemeColors,
    EChartsTypeChoices,
    queryset_to_nested_dict_keys_as_series,
)
from nautobot.core.views.mixins import PERMISSIONS_ACTION_MAP, ObjectDataComplianceViewMixin
from nautobot.dcim.models import Device
from nautobot.dcim.views import DeviceUIViewSet
from nautobot.extras.models import Job, JobResult
from rest_framework.decorators import action
from rest_framework.response import Response

from nautobot_golden_config import details, filters, forms, models, tables
from nautobot_golden_config.api import serializers
from nautobot_golden_config.utilities import backup_diff_read, config_diff, constant
from nautobot_golden_config.utilities.config_postprocessing import get_config_postprocessing
from nautobot_golden_config.utilities.graphql import graph_ql_query
from nautobot_golden_config.utilities.helper import add_message, calculate_aggr_percentage, get_device_to_settings_map

# TODO: Future #4512
PERMISSIONS_ACTION_MAP.update(
    {
        "backup": "view",
        "compliance": "view",
        "intended": "view",
        "sotagg": "view",
        "postprocessing": "view",
        "devicetab": "view",
    }
)
LOGGER = logging.getLogger(__name__)

#
# GoldenConfig
#


class GoldenConfigUIViewSet(  # pylint: disable=abstract-method
    views.ObjectDetailViewMixin,
    views.ObjectDestroyViewMixin,
    views.ObjectBulkDestroyViewMixin,
    views.ObjectListViewMixin,  # TODO: Changing the order of the mixins breaks things... why?
    ObjectDataComplianceViewMixin,  # TODO: Import from views after nautobot release
):
    """Views for the GoldenConfig model."""

    bulk_update_form_class = forms.GoldenConfigBulkEditForm
    table_class = tables.GoldenConfigTable
    filterset_class = filters.GoldenConfigFilterSet
    filterset_form_class = forms.GoldenConfigFilterForm
    queryset = models.GoldenConfig.objects.all()
    serializer_class = serializers.GoldenConfigSerializer
    action_buttons = ("export",)
    object_detail_content = details.golden_config

    def __init__(self, *args, **kwargs):
        """Used to set default variables on GoldenConfigUIViewSet."""
        super().__init__(*args, **kwargs)
        self.device = None
        self.output = ""
        self.structured_format = None
        self.title_name = None
        self.is_modal = None
        self.config_details = None
        self.action_template_name = None

    def filter_queryset(self, queryset):
        """Add a warning message when GoldenConfig Table is out of sync."""
        queryset = super().filter_queryset(queryset)
        # Only adding a message when no filters are applied
        if self.filter_params:
            return queryset

        sync_job = Job.objects.get(
            module_name="nautobot_golden_config.jobs", job_class_name="SyncGoldenConfigWithDynamicGroups"
        )
        sync_job_url = f"<a href='{reverse('extras:job_run', kwargs={'pk': sync_job.pk})}'>{sync_job.name}</a>"
        out_of_sync_message = format_html(
            "The expected devices and actual devices here are not in sync. "
            f"Running the job {sync_job_url} will put it back in sync."
        )

        gc_dynamic_group_device_pks = models.GoldenConfig.get_dynamic_group_device_pks()
        gc_device_pks = models.GoldenConfig.get_golden_config_device_ids()
        if gc_dynamic_group_device_pks != gc_device_pks:
            messages.warning(self.request, message=out_of_sync_message)

        return queryset

    def _get_device_context(self, instance):
        return {
            "Backup Config": reverse(
                "plugins:nautobot_golden_config:goldenconfig_backup", kwargs={"pk": instance.device.pk}
            ),
            "Intended Config": reverse(
                "plugins:nautobot_golden_config:goldenconfig_intended", kwargs={"pk": instance.device.pk}
            ),
            "Compliance Config": reverse(
                "plugins:nautobot_golden_config:goldenconfig_compliance", kwargs={"pk": instance.device.pk}
            ),
        }

    def get_extra_context(self, request, instance=None):
        """Get extra context data."""
        context = super().get_extra_context(request, instance)
        if self.action == "retrieve":
            context["device_object"] = self._get_device_context(instance)
        context["compliance"] = constant.ENABLE_COMPLIANCE
        context["backup"] = constant.ENABLE_BACKUP
        context["intended"] = constant.ENABLE_INTENDED
        jobs = []
        jobs.append(["BackupJob", constant.ENABLE_BACKUP])
        jobs.append(["IntendedJob", constant.ENABLE_INTENDED])
        jobs.append(["ComplianceJob", constant.ENABLE_COMPLIANCE])
        add_message(jobs, request)
        return context

    def _pre_helper(self, pk, request):
        self.device = Device.objects.get(pk=pk)
        if request.GET.get("config_plan_id"):
            self.config_details = models.ConfigPlan.objects.get(id=request.GET.get("config_plan_id"))
        else:
            self.config_details = models.GoldenConfig.objects.filter(device=self.device).first()
        self.action_template_name = "nautobot_golden_config/goldenconfig_details.html"
        self.structured_format = "json"
        self.is_modal = False
        if request.GET.get("modal") == "true":
            self.action_template_name = "nautobot_golden_config/goldenconfig_detailsmodal.html"
            self.is_modal = True

    def _post_render(self, request):
        context = {
            "output": self.output,
            "device": self.device,
            "device_name": self.device.name,
            "format": self.structured_format,
            "title_name": self.title_name,
            "is_modal": self.is_modal,
        }
        return render(request, self.action_template_name, context)

    @action(detail=True, methods=["get"])
    def backup(self, request, pk, *args, **kwargs):
        """Additional action to handle backup_config."""
        self._pre_helper(pk, request)
        self.output = self.config_details.backup_config
        self.structured_format = "cli"
        self.title_name = "Backup Configuration Details"
        return self._post_render(request)

    @action(detail=True, methods=["get"])
    def intended(self, request, pk, *args, **kwargs):
        """Additional action to handle intended_config."""
        self._pre_helper(pk, request)
        self.output = self.config_details.intended_config
        self.structured_format = "cli"
        self.title_name = "Intended Configuration Details"
        return self._post_render(request)

    @action(detail=True, methods=["get"])
    def postprocessing(self, request, pk, *args, **kwargs):
        """Additional action to handle postprocessing."""
        self._pre_helper(pk, request)
        self.output = get_config_postprocessing(self.config_details, request)
        self.structured_format = "cli"
        self.title_name = "Post Processing"
        return self._post_render(request)

    @action(detail=True, methods=["get"])
    def sotagg(self, request, pk, *args, **kwargs):
        """Additional action to handle sotagg."""
        self._pre_helper(pk, request)
        self.structured_format = "json"
        if request.GET.get("format") in ["json", "yaml"]:
            self.structured_format = request.GET.get("format")

        settings = get_device_to_settings_map(queryset=Device.objects.filter(pk=self.device.pk))
        if self.device.id in settings:
            sot_agg_query_setting = settings[self.device.id].sot_agg_query
            if sot_agg_query_setting is not None:
                _, self.output = graph_ql_query(request, self.device, sot_agg_query_setting.query)
            else:
                self.output = {"Error": "No saved `GraphQL Query` query was configured in the `Golden Config Setting`"}
        else:
            raise ObjectDoesNotExist(f"{self.device.name} does not map to a Golden Config Setting.")

        if self.structured_format == "yaml":
            self.output = yaml.dump(json.loads(json.dumps(self.output)), default_flow_style=False)
        else:
            self.output = json.dumps(self.output, indent=4)
        self.title_name = "Aggregate Data"
        return self._post_render(request)

    @action(detail=True, methods=["get"])
    def compliance(self, request, pk, *args, **kwargs):
        """Additional action to handle compliance."""
        self._pre_helper(pk, request)

        self.output = self.config_details.compliance_config
        if self.config_details.backup_last_success_date:
            backup_date = str(self.config_details.backup_last_success_date.strftime("%b %d %Y"))
        else:
            backup_date = make_aware(datetime.now()).strftime("%b %d %Y")
        if self.config_details.intended_last_success_date:
            intended_date = str(self.config_details.intended_last_success_date.strftime("%b %d %Y"))
        else:
            intended_date = make_aware(datetime.now()).strftime("%b %d %Y")

        diff_type = "File"
        self.structured_format = "diff"

        if self.output == "":
            # This is used if all config snippets are in compliance and no diff exist.
            self.output = f"--- Backup {diff_type} - " + backup_date + f"\n+++ Intended {diff_type} - " + intended_date
        else:
            first_occurence = self.output.index("@@")
            second_occurence = self.output.index("@@", first_occurence)
            # This is logic to match diff2html's expected input.
            self.output = (
                f"--- Backup {diff_type} - "
                + backup_date
                + f"\n+++ Intended {diff_type} - "
                + intended_date
                + "\n"
                + self.output[first_occurence:second_occurence]
                + "@@"
                + self.output[second_occurence + 2 :]  # noqa: E203
            )
        self.title_name = "Compliance Details"
        return self._post_render(request)


#
# ConfigCompliance
#


def _get_filtered_compliance_device_ids(query_params):
    """Return a deduplicated list of device IDs from ConfigCompliance records that match query_params.

    Uses ConfigComplianceFilterSet so every filter condition (location, status, platform, etc.)
    hits the same device JOIN chain. .order_by() strips default ordering that would otherwise
    add extra columns to GROUP BY; .distinct() ensures each device_id appears exactly once
    even when multiple filter conditions would otherwise produce duplicate rows.
    """
    return list(
        filters.ConfigComplianceFilterSet(query_params, models.ConfigCompliance.objects.all())
        .qs.order_by()
        .values_list("device_id", flat=True)
        .distinct()
    )


def _get_feature_compliance_queryset(device_ids):
    """Return annotated ComplianceFeature queryset for the given device IDs.

    Annotates each ComplianceFeature with count, compliant, non_compliant, and comp_percent
    scoped to the provided device IDs. Uses filter= on Count/Sum so that no rows are inflated
    by JOIN multiplicity. comp_percent is NULL (not zero) when count is zero, preventing
    division-by-zero.
    """
    device_filter = Q(feature__rule__device__in=device_ids)
    return models.ComplianceFeature.objects.annotate(
        count=Count("feature__rule", filter=device_filter),
        compliant=Coalesce(Sum("feature__rule__compliance_int", filter=device_filter), 0),
        non_compliant=Count("feature__rule", filter=device_filter)
        - Coalesce(Sum("feature__rule__compliance_int", filter=device_filter), 0),
        comp_percent=ExpressionWrapper(
            100.0
            * Coalesce(Sum("feature__rule__compliance_int", filter=device_filter), 0)
            / NullIf(Count("feature__rule", filter=device_filter), Value(0)),
            output_field=FloatField(),
        ),
    ).order_by("-comp_percent")


def get_compliance_overview_queryset(query_params):
    """Return an annotated ComplianceFeature queryset scoped to devices matching query_params.

    Combines _get_filtered_compliance_device_ids and _get_feature_compliance_queryset:
    1. Extract a deduplicated list of device IDs from ConfigCompliance records that satisfy
       the filter — this is a plain DISTINCT SELECT, so no aggregation runs here.
    2. Annotate ComplianceFeature using those IDs as a CASE WHEN inside Count/Sum (the
       filter= parameter), never as a WHERE JOIN — so rows cannot multiply before GROUP BY.

    This separation is what prevents Cartesian-product inflation when any filter path
    traverses a one-to-many or many-to-many relationship before aggregation.
    """
    device_ids = _get_filtered_compliance_device_ids(query_params)
    return _get_feature_compliance_queryset(device_ids)


class ConfigComplianceUIViewSet(  # pylint: disable=abstract-method
    views.ObjectDetailViewMixin,
    views.ObjectDestroyViewMixin,
    views.ObjectBulkDestroyViewMixin,
    views.ObjectListViewMixin,
):
    """Views for the ConfigCompliance model."""

    filterset_class = filters.ConfigComplianceFilterSet
    filterset_form_class = forms.ConfigComplianceFilterForm
    queryset = models.ConfigCompliance.objects.all().order_by("device__name")
    serializer_class = serializers.ConfigComplianceSerializer
    table_class = tables.ConfigComplianceTable
    table_delete_class = tables.ConfigComplianceDeleteTable

    custom_action_permission_map = None
    action_buttons = ("export",)
    object_detail_content = details.config_compliance

    def __init__(self, *args, **kwargs):
        """Used to set default variables on ConfigComplianceUIViewSet."""
        super().__init__(*args, **kwargs)
        self.pk_list = None
        self.report_context = None
        self.store_table = None  # Used to store the table for bulk delete. No longer required in Nautobot 2.3.11

    def get_extra_context(self, request, instance=None):
        """A ConfigCompliance helper function to warn if the Job is not enabled to run."""
        context = super().get_extra_context(request, instance)
        # TODO Remove when dropping support for Nautobot < 2.3.11
        if self.action == "bulk_destroy":
            context["table"] = self.store_table

        context["compliance"] = constant.ENABLE_COMPLIANCE
        context["backup"] = constant.ENABLE_BACKUP
        context["intended"] = constant.ENABLE_INTENDED
        add_message([["ComplianceJob", constant.ENABLE_COMPLIANCE]], request)
        return context

    def alter_queryset(self, request):
        """Build actual runtime queryset as the build time queryset of table `pivoted`."""
        # Super because alter_queryset() calls get_queryset(), which is what calls queryset.restrict()
        self.queryset = super().alter_queryset(request)
        return pivot(
            self.queryset,
            ["device", "device__name"],
            "rule__feature__slug",
            "compliance_int",
            aggregation=Max,
        )

    def perform_bulk_destroy(self, request, **kwargs):
        """Overwrite perform_bulk_destroy to handle special use case in which the UI shows devices but want to delete ConfigCompliance objects."""
        model = self.queryset.model
        # Are we deleting *all* objects in the queryset or just a selected subset?
        if request.POST.get("_all"):
            filter_params = self.get_filter_params(request)
            if not filter_params:
                compliance_objects = model.objects.only("pk").all().values_list("pk", flat=True)
            elif self.filterset_class is None:
                raise NotImplementedError("filterset_class must be defined to use _all")
            else:
                compliance_objects = self.filterset_class(filter_params, model.objects.only("pk")).qs
            # When selecting *all* the resulting request args are ConfigCompliance object PKs
            self.pk_list = [item[0] for item in self.queryset.filter(pk__in=compliance_objects).values_list("id")]
        elif "_confirm" not in request.POST:
            # When it is not being confirmed, the pk's are the device objects.
            device_objects = request.POST.getlist("pk")
            self.pk_list = [item[0] for item in self.queryset.filter(device__pk__in=device_objects).values_list("id")]
        else:
            self.pk_list = request.POST.getlist("pk")

        form_class = self.get_form_class(**kwargs)
        data = {}
        if "_confirm" in request.POST:
            form = form_class(request.POST)
            if form.is_valid():
                return self.form_valid(form)
            return self.form_invalid(form)

        table = self.table_delete_class(self.queryset.filter(pk__in=self.pk_list), orderable=False)

        if not table.rows:
            messages.warning(
                request,
                f"No {self.queryset.model._meta.verbose_name_plural} were selected for deletion.",
            )
            return redirect(self.get_return_url(request))

        # TODO Remove when dropping support for Nautobot < 2.3.11
        self.store_table = table

        if not request.POST.get("_all"):
            data.update({"table": table, "total_objs_to_delete": len(table.rows)})
        else:
            data.update({"table": None, "delete_all": True, "total_objs_to_delete": len(table.rows)})
        return Response(data)

    @action(detail=True, methods=["get"])
    def devicetab(self, request, pk, *args, **kwargs):
        """Additional action to handle backup_config."""
        device = Device.objects.get(pk=pk)
        context = {}
        compliance_details = models.ConfigCompliance.objects.filter(device=device)
        context["compliance_details"] = compliance_details
        if request.GET.get("compliance") == "compliant":
            context["compliance_filter"] = "compliant"
            context["compliance_details"] = compliance_details.filter(compliance=True)
        elif request.GET.get("compliance") == "non-compliant":
            context["compliance_filter"] = "non-compliant"
            context["compliance_details"] = compliance_details.filter(compliance=False)

        context["active_tab"] = request.GET.get("tab")
        context["device"] = device
        context["object"] = device
        context["object_detail_content"] = DeviceUIViewSet.object_detail_content
        context["verbose_name"] = "Device"

        return render(request, "nautobot_golden_config/configcompliance_devicetab.html", context)

    @action(detail=False, methods=["get"], custom_view_base_action="view")
    def overview(self, request, *args, **kwargs):  # pylint: disable=too-many-locals
        """Custom action to show the visual report of the compliance stats."""
        # Basic Setup
        context = {}
        theme = request.COOKIES["theme"] if "theme" in request.COOKIES else "light"
        if theme not in ["light", "dark"]:
            theme = "light"

        context["filter_form"] = forms.ComplianceFeatureFilterFormAlt(request.GET)
        context["action_buttons"] = ("export",)

        # Queryset & Filter Setup

        filtered_device_ids = _get_filtered_compliance_device_ids(request.GET)
        feature_qs = _get_feature_compliance_queryset(filtered_device_ids)

        # Table Setup
        table = tables.ConfigComplianceGlobalFeatureTable(feature_qs, user=request.user)
        paginate = {
            "paginator_class": views.EnhancedPaginator,
            "per_page": views.get_paginate_count(request),
        }
        RequestConfig(request, paginate).configure(table)
        context["table"] = table

        # Bar Chart Setup
        chart_data = queryset_to_nested_dict_keys_as_series(
            queryset=feature_qs,
            record_key="slug",
            value_keys=["compliant", "non_compliant"],
        )

        bar_chart_panel = EChartsPanel(
            label="Compliance Overview",
            weight=100,
            chart_kwargs={
                "chart_type": EChartsTypeChoices.BAR,
                "header": "Compliance per Feature",
                "description": "This shows the compliance status for each feature.",
                "theme_colors": EChartsThemeColors.LIGHTER_GREEN_RED_COLORS,
                "data": chart_data,
            },
        )
        context["bar_chart_panel"] = [bar_chart_panel]

        # Pie Charts Setup
        # Re-use filtered_device_ids to avoid JOIN chains or Cartesian product
        cc_qs = models.ConfigCompliance.objects.filter(device_id__in=filtered_device_ids)

        # Device pie: total = distinct devices with ≥1 compliance record;
        # compliants = devices with zero non-compliant records.
        device_total = cc_qs.values("device_id").distinct().count()
        device_non_compliant = cc_qs.filter(compliance=False).values("device_id").distinct().count()
        device_aggr = calculate_aggr_percentage(
            {"total": device_total, "compliants": device_total - device_non_compliant}
        )

        pie_device_panel = EChartsPanel(
            label="Device Compliance Overview",
            weight=100,
            chart_kwargs={
                "chart_type": EChartsTypeChoices.PIE,
                "header": "Compliant vs Non-Compliant Devices",
                "description": "This shows the compliance status on a device level.",
                "theme_colors": EChartsThemeColors.LIGHTER_GREEN_RED_COLORS,
                "data": {
                    "Device Compliance": {
                        "Compliant": device_aggr["compliants"],
                        "Non-Compliant": device_aggr["non_compliants"],
                    }
                },
            },
        )
        context["pie_device_panel"] = [pie_device_panel]

        # Feature pie: total = all compliance records for filtered devices;
        # compliants = those marked compliant.
        feature_aggr = calculate_aggr_percentage(
            cc_qs.aggregate(total=Count("id"), compliants=Count("id", filter=Q(compliance=True)))
        )

        pie_feature_panel = EChartsPanel(
            label="Feature Compliance Overview",
            weight=100,
            chart_kwargs={
                "chart_type": EChartsTypeChoices.PIE,
                "header": "Compliant vs Non-Compliant Features",
                "description": "This shows the compliance status on a feature level.",
                "theme_colors": EChartsThemeColors.LIGHTER_GREEN_RED_COLORS,
                "data": {
                    "Feature Compliance": {
                        "Compliant": feature_aggr["compliants"],
                        "Non-Compliant": feature_aggr["non_compliants"],
                    }
                },
            },
        )
        context["pie_feature_panel"] = [pie_feature_panel]

        return Response(context)


class ComplianceFeatureUIViewSet(views.NautobotUIViewSet):
    """Views for the ComplianceFeature model."""

    bulk_update_form_class = forms.ComplianceFeatureBulkEditForm
    filterset_class = filters.ComplianceFeatureFilterSet
    filterset_form_class = forms.ComplianceFeatureFilterForm
    form_class = forms.ComplianceFeatureForm
    queryset = models.ComplianceFeature.objects.all()
    serializer_class = serializers.ComplianceFeatureSerializer
    table_class = tables.ComplianceFeatureTable
    lookup_field = "pk"
    object_detail_content = details.compliance_feature

    def get_extra_context(self, request, instance=None):
        """A ComplianceFeature helper function to warn if the Job is not enabled to run."""
        add_message([["ComplianceJob", constant.ENABLE_COMPLIANCE]], request)
        return super().get_extra_context(request, instance)


class ComplianceRuleUIViewSet(views.NautobotUIViewSet):
    """Views for the ComplianceRule model."""

    bulk_update_form_class = forms.ComplianceRuleBulkEditForm
    filterset_class = filters.ComplianceRuleFilterSet
    filterset_form_class = forms.ComplianceRuleFilterForm
    form_class = forms.ComplianceRuleForm
    queryset = models.ComplianceRule.objects.all()
    serializer_class = serializers.ComplianceRuleSerializer
    table_class = tables.ComplianceRuleTable
    lookup_field = "pk"
    object_detail_content = details.compliance_rule

    def get_extra_context(self, request, instance=None):
        """A ComplianceRule helper function to warn if the Job is not enabled to run."""
        add_message([["ComplianceJob", constant.ENABLE_COMPLIANCE]], request)
        return super().get_extra_context(request, instance)


class GoldenConfigSettingUIViewSet(views.NautobotUIViewSet):
    """Views for the GoldenConfigSetting model."""

    bulk_update_form_class = forms.GoldenConfigSettingBulkEditForm
    filterset_class = filters.GoldenConfigSettingFilterSet
    filterset_form_class = forms.GoldenConfigSettingFilterForm
    form_class = forms.GoldenConfigSettingForm
    queryset = models.GoldenConfigSetting.objects.all()
    serializer_class = serializers.GoldenConfigSettingSerializer
    table_class = tables.GoldenConfigSettingTable
    lookup_field = "pk"
    object_detail_content = details.golden_config_setting
    extra_buttons = ("clone",)

    def get_extra_context(self, request, instance=None):
        """A GoldenConfig helper function to warn if the Job is not enabled to run."""
        context = super().get_extra_context(request, instance)
        if self.action == "retrieve":
            dg = getattr(instance, "dynamic_group", None)
            context["dg_data"] = {"Dynamic Group": dg, "Filter Query Logic": dg.filter, "Scope of Devices": dg}

        jobs = []
        jobs.append(["BackupJob", constant.ENABLE_BACKUP])
        jobs.append(["IntendedJob", constant.ENABLE_INTENDED])
        jobs.append(["DeployConfigPlans", constant.ENABLE_DEPLOY])
        jobs.append(["ComplianceJob", constant.ENABLE_COMPLIANCE])
        jobs.append(
            [
                "AllGoldenConfig",
                [
                    constant.ENABLE_BACKUP,
                    constant.ENABLE_COMPLIANCE,
                    constant.ENABLE_DEPLOY,
                    constant.ENABLE_INTENDED,
                    constant.ENABLE_SOTAGG,
                ],
            ]
        )
        jobs.append(
            [
                "AllDevicesGoldenConfig",
                [
                    constant.ENABLE_BACKUP,
                    constant.ENABLE_COMPLIANCE,
                    constant.ENABLE_DEPLOY,
                    constant.ENABLE_INTENDED,
                    constant.ENABLE_SOTAGG,
                ],
            ]
        )
        add_message(jobs, request)
        return context


class ConfigRemoveUIViewSet(views.NautobotUIViewSet):
    """Views for the ConfigRemove model."""

    bulk_update_form_class = forms.ConfigRemoveBulkEditForm
    filterset_class = filters.ConfigRemoveFilterSet
    filterset_form_class = forms.ConfigRemoveFilterForm
    form_class = forms.ConfigRemoveForm
    queryset = models.ConfigRemove.objects.all()
    serializer_class = serializers.ConfigRemoveSerializer
    table_class = tables.ConfigRemoveTable
    lookup_field = "pk"
    object_detail_content = details.config_remove

    def get_extra_context(self, request, instance=None):
        """A ConfigRemove helper function to warn if the Job is not enabled to run."""
        add_message([["BackupJob", constant.ENABLE_BACKUP]], request)
        return super().get_extra_context(request, instance)


class ConfigReplaceUIViewSet(views.NautobotUIViewSet):
    """Views for the ConfigReplace model."""

    bulk_update_form_class = forms.ConfigReplaceBulkEditForm
    filterset_class = filters.ConfigReplaceFilterSet
    filterset_form_class = forms.ConfigReplaceFilterForm
    form_class = forms.ConfigReplaceForm
    queryset = models.ConfigReplace.objects.all()
    serializer_class = serializers.ConfigReplaceSerializer
    table_class = tables.ConfigReplaceTable
    lookup_field = "pk"
    object_detail_content = details.config_replace

    def get_extra_context(self, request, instance=None):
        """A ConfigReplace helper function to warn if the Job is not enabled to run."""
        add_message([["BackupJob", constant.ENABLE_BACKUP]], request)
        return super().get_extra_context(request, instance)


class RemediationSettingUIViewSet(views.NautobotUIViewSet):
    """Views for the RemediationSetting model."""

    # bulk_create_form_class = forms.RemediationSettingCSVForm
    bulk_update_form_class = forms.RemediationSettingBulkEditForm
    filterset_class = filters.RemediationSettingFilterSet
    filterset_form_class = forms.RemediationSettingFilterForm
    form_class = forms.RemediationSettingForm
    queryset = models.RemediationSetting.objects.all()
    serializer_class = serializers.RemediationSettingSerializer
    table_class = tables.RemediationSettingTable
    lookup_field = "pk"
    object_detail_content = details.config_remediation

    def get_extra_context(self, request, instance=None):
        """A RemediationSetting helper function to warn if the Job is not enabled to run."""
        add_message([["ComplianceJob", constant.ENABLE_COMPLIANCE]], request)
        return super().get_extra_context(request, instance)


class ConfigPlanUIViewSet(views.NautobotUIViewSet):
    """Views for the ConfigPlan model."""

    bulk_update_form_class = forms.ConfigPlanBulkEditForm
    filterset_class = filters.ConfigPlanFilterSet
    filterset_form_class = forms.ConfigPlanFilterForm
    form_class = forms.ConfigPlanForm
    queryset = models.ConfigPlan.objects.all()
    serializer_class = serializers.ConfigPlanSerializer
    table_class = tables.ConfigPlanTable
    lookup_field = "pk"
    action_buttons = ("add",)
    update_form_class = forms.ConfigPlanUpdateForm
    object_detail_content = details.config_plan

    def alter_queryset(self, request):
        """Build actual runtime queryset to automatically remove `Completed` by default."""
        if "Completed" not in request.GET.getlist("status"):
            return self.queryset.exclude(status__name="Completed")
        return self.queryset

    def get_extra_context(self, request, instance=None):
        """A ConfigPlan helper function to warn if the Job is not enabled to run."""
        context = super().get_extra_context(request, instance)
        jobs = []
        jobs.append(["GenerateConfigPlans", constant.ENABLE_PLAN])
        jobs.append(["DeployConfigPlans", constant.ENABLE_DEPLOY])
        jobs.append(["DeployConfigPlanJobButtonReceiver", constant.ENABLE_DEPLOY])
        add_message(jobs, request)
        return context


class ConfigPlanBulkDeploy(views.ObjectPermissionRequiredMixin, View):
    """View to run the Config Plan Deploy Job."""

    queryset = models.ConfigPlan.objects.all()

    def get_required_permission(self):
        """Permissions required for the view."""
        return "extras.run_job"

    # Once https://github.com/nautobot/nautobot/issues/4529 is addressed, can turn this on.
    # Permalink reference: https://github.com/nautobot/nautobot-app-golden-config/blob/017d5e1526fa9f642b9e02bfc7161f27d4948bef/nautobot_golden_config/views.py#L609-L612
    # @action(detail=False, methods=["post"])
    # def bulk_deploy(self, request):
    def post(self, request):
        """Enqueue the job and redirect to the job results page."""
        config_plan_pks = request.POST.getlist("pk")
        if not config_plan_pks:
            messages.warning(request, "No Config Plans selected for deployment.")
            return redirect("plugins:nautobot_golden_config:configplan_list")

        job_data = {"config_plan": config_plan_pks}
        job = Job.objects.get(name="Generate Config Plans")

        job_result = JobResult.enqueue_job(
            job,
            request.user,
            data=job_data,
            **job.job_class.serialize_data(request),
        )
        return redirect(job_result.get_absolute_url())


class GenerateIntendedConfigView(PermissionRequiredMixin, TemplateView):
    """View to generate the intended configuration."""

    template_name = "nautobot_golden_config/generate_intended_config.html"
    permission_required = ["dcim.view_device", "extras.view_gitrepository"]

    def get_context_data(self, **kwargs):
        """Get the context data for the view."""
        context = super().get_context_data(**kwargs)
        context["form"] = forms.GenerateIntendedConfigForm()
        return context


def _resolve_device_by_name_or_ip(query, user):
    """Resolve a device by exact name, else by primary or interface IP address.

    Args:
        query (str): A device name or an IP address (an optional ``/mask`` is stripped).
        user: The requesting user; results are scoped with ``restrict(user, "view")``.

    Returns:
        Device | None: The first matching device the user may view, or ``None``.
    """
    if not query:
        return None
    query = query.strip()
    devices = Device.objects.restrict(user, "view")
    device = devices.filter(name=query).first()
    if device is not None:
        return device
    ip = query.split("/")[0].strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        # Not an IP (e.g. a partial name like "demo"): don't feed it to the IPAM ``__host`` lookups, which
        # raise ValidationError on non-IP input. The exact-name match above is the only name matching this
        # helper does -- the device dropdown is what users search names with -- so this is a miss.
        return None
    return (
        devices.filter(primary_ip4__host=ip).first()
        or devices.filter(primary_ip6__host=ip).first()
        or devices.filter(interfaces__ip_addresses__host=ip).distinct().first()
    )


def build_backup_diff_context(device, request, extra_params=None):
    """Build the shared Backup History Diff context for a device (both the device tab and the tool page).

    Picks the two commits to compare (``?a=<older>&b=<newer>``, defaulting to the two most recent) and
    computes the diff.

    The two dropdowns are rendered as a plain GET form posting back to ``request.path``, so ``history`` and
    the chosen entries are all the template needs -- ``compare_params`` carries whatever else that page must
    preserve (``tab`` or ``device``) as hidden inputs. This is what lets the selectors work with JavaScript
    disabled; the script only auto-submits the form to save a click.

    Args:
        device (Device): The device whose backup history is shown.
        request (HttpRequest): The current request (source of the ``a``/``b`` selection and the form target).
        extra_params (dict | None): Query params this page must preserve (e.g. ``tab`` or ``device``).

    Returns:
        dict: Template context (history, chosen original/modified, form params, diff rows).
    """
    history = backup_diff_read.history(device)
    by_sha = {entry["sha"]: entry for entry in history}

    # Default the "modified" (newer) side to the latest commit and "original" (older) to the one before.
    modified = by_sha.get(request.GET.get("b", "")) or (history[0] if history else None)
    original = by_sha.get(request.GET.get("a", ""))
    if original is None and modified is not None:
        idx = history.index(modified)
        original = history[idx + 1] if idx + 1 < len(history) else None
    # Always present chronologically: original (left) = older, modified (right) = newer.
    if original and modified and original["date"] > modified["date"]:
        original, modified = modified, original

    diff_rows, additions, deletions, diff_too_large = backup_diff_read.backup_diff(device, original, modified)

    return {
        "device": device,
        "history": history,
        "original": original,
        "modified": modified,
        # The form posts back to the page it is on; no need to reverse a URL we are already serving.
        "compare_url": request.path,
        "compare_params": {key: val for key, val in (extra_params or {}).items() if val},
        "diff_rows": diff_rows,
        "additions": additions,
        "deletions": deletions,
        "diff_too_large": diff_too_large,
    }


class BackupHistoryDiffView(PermissionRequiredMixin, View):
    """Per-device "Backup History Diff" tab on the device detail page.

    Git-native side-by-side diff of a device's backup config history: reads the device's backup file
    straight from the backup Git repository (no config text stored in the database) and diffs any two
    commits, defaulting to the two most recent. Works for any vendor/format because the diff is a plain
    line-based text diff.
    """

    permission_required = ["dcim.view_device", "extras.view_gitrepository"]

    def get(self, request, pk):
        """Render the Backup History Diff tab for a single device."""
        device = get_object_or_404(Device.objects.restrict(request.user, "view"), pk=pk)
        context = build_backup_diff_context(device, request, {"tab": request.GET.get("tab", "")})
        context.update(
            {
                "object": device,
                "active_tab": request.GET.get("tab"),
                "object_detail_content": DeviceUIViewSet.object_detail_content,
                "verbose_name": "Device",
            }
        )
        return _render_backup_diff(request, "nautobot_golden_config/backuphistorydiff_devicetab.html", context)


def _render_backup_diff(request, template, context):
    """Render a Backup History Diff page with ``Vary: HX-Request`` set.

    These pages extend Nautobot's base template, which renders less markup for an HTMX request than for a
    full navigation. Without advertising that the response varies on ``HX-Request``, a cache keyed only on
    cookies can hand a browser the partial it stored for an HTMX swap when the user presses Back -- the
    familiar "back button shows a fragment" failure. Nautobot core does the same thing in its own views
    (``core.views.generic`` and ``extras.views``); there is no middleware doing it globally, so any view
    rendering the shared chrome has to set it.
    """
    response = render(request, template, context)
    patch_vary_headers(response, ["HX-Request"])
    return response


def _backup_fleet_context(request):
    """Build the fleet-wide backup inventory shown on the Backup History Diff landing page.

    One row per device -- that device's most recent backup -- filterable by device name and by when it was
    last backed up, sorted and paginated in the database.

    This needs a queryset, which only exists when the backup-diff index is populated. With the index off
    (the default), there is nothing to filter in SQL, so the page falls back to the git-native list and
    says so rather than silently offering controls that would do nothing.

    Args:
        request (HttpRequest): The current request; supplies the filter params and the user.

    Returns:
        dict: ``{"table": ...}`` plus the filter form when index-backed, else ``{"recent_changes": [...]}``.
    """
    if not constant.ENABLE_BACKUP_DIFF_INDEX:
        # The git-native list walks every in-scope device, so its cost is linear in fleet size. Past the
        # ceiling, refuse rather than hand the operator a ten-second page; the per-device diff itself is
        # unaffected and stays fast, so only this fleet list is withheld.
        fleet_size = config_diff.backup_scope_device_count()
        if fleet_size > constant.BACKUP_DIFF_MAX_FALLBACK_FLEET:
            return {
                "recent_changes": [],
                "index_disabled": True,
                "fleet_too_large": fleet_size,
                "fallback_limit": constant.BACKUP_DIFF_MAX_FALLBACK_FLEET,
            }
        return {
            "recent_changes": backup_diff_read.recent_changes(request.user),
            "index_disabled": True,
        }

    queryset = models.BackupVersion.objects.for_devices_viewable_by(request.user).latest_per_device()
    queryset = filters.BackupVersionFilterSet(request.GET, queryset).qs.select_related("device", "repository")
    table = tables.BackupVersionTable(queryset, user=request.user)
    RequestConfig(
        request,
        {"paginator_class": views.EnhancedPaginator, "per_page": views.get_paginate_count(request)},
    ).configure(table)
    return {
        "table": table,
        "index_disabled": False,
    }


class BackupVersionBulkDeleteView(PermissionRequiredMixin, View):
    """Two-step bulk delete of backup *index* records: pick devices, then pick which versions to drop.

    Step 1 posts the device rows selected on the fleet table; this view expands them into every
    ``BackupVersion`` those devices have and renders them for selection. Step 2 posts the chosen version
    PKs and deletes them.

    Two invariants, both enforced server-side rather than only in the template:

    * A device's most recent version is never deletable. It is what the diff and history views default to,
      so dropping it would blank the feature for that device.
    * Only index records are removed. The configurations themselves live in the backup Git repository and
      are never touched, so a delete here is recoverable by re-indexing -- it is not data loss.
    """

    permission_required = ["dcim.view_device", "nautobot_golden_config.delete_backupversion"]

    @staticmethod
    def _uuids(values):
        """Return only the values that parse as UUIDs.

        Every pk here arrives from a form field, and a UUID column rejects a non-UUID by raising
        ``ValidationError`` -- which would surface as a 500 rather than a message. Filtering first means a
        malformed or hand-crafted submission is treated as "selected nothing", not as a server error.
        """
        valid = []
        for value in values:
            try:
                valid.append(uuid.UUID(str(value)))
            except (AttributeError, TypeError, ValueError):
                continue
        return valid

    def _versions_for(self, request, device_pks):
        """Return (versions, latest_pks) for the given devices, scoped to what the user may view."""
        versions = (
            models.BackupVersion.objects.for_devices_viewable_by(request.user)
            .filter(device__in=device_pks)
            .select_related("device")
            .order_by("device__name", "-authored_date", "-commit_sha")
        )
        latest_pks = set(
            models.BackupVersion.objects.for_devices_viewable_by(request.user)
            .filter(device__in=device_pks)
            .latest_per_device()
            .values_list("pk", flat=True)
        )
        return versions, latest_pks

    def get(self, request):
        """Render the paginated confirmation page for the devices named in ``?device=``.

        A GET rather than a rendered POST response so the page paginates like any other list: Nautobot's
        paginator include carries the existing query string forward, so ``?device=...&page=2`` just works
        and the page is reloadable and shareable. Without pagination this view rendered every version of
        every selected device in one response -- and versions accumulating is the exact reason retention
        exists, so "50 devices x thousands of versions" is a reachable page, not a hypothetical one.
        """
        redirect_url = reverse("plugins:nautobot_golden_config:backuphistorydiff")
        device_pks = self._uuids(request.GET.getlist("device"))
        if not device_pks:
            messages.warning(request, "Select at least one device before choosing versions to delete.")
            return redirect(redirect_url)

        versions, latest_pks = self._versions_for(request, device_pks)
        paginator = views.EnhancedPaginator(versions, views.get_paginate_count(request))
        page = paginator.get_page(request.GET.get("page"))
        return _render_backup_diff(
            request,
            "nautobot_golden_config/backupversion_bulk_delete.html",
            {
                "versions": page.object_list,
                "paginator": paginator,
                "page": page,
                "latest_pks": latest_pks,
                "device_count": len(device_pks),
                "total_versions": paginator.count,
                "return_url": redirect_url,
            },
        )

    def post(self, request):
        """Expand selected devices into their versions, or delete the versions that were selected."""
        redirect_url = reverse("plugins:nautobot_golden_config:backuphistorydiff")

        if request.POST.get("confirm"):
            selected = self._uuids(request.POST.getlist("version_pk"))
            if not selected:
                messages.warning(request, "No backup versions were selected, so nothing was deleted.")
                return redirect(redirect_url)
            candidates = models.BackupVersion.objects.for_devices_viewable_by(request.user).filter(pk__in=selected)
            device_pks = list(candidates.values_list("device_id", flat=True).distinct())
            _, latest_pks = self._versions_for(request, device_pks)
            # Re-derive the protected set here rather than trusting the form: a crafted POST could
            # otherwise include a latest-version PK that the template rendered as disabled.
            deletable = [version for version in candidates if version.pk not in latest_pks]
            protected = len(selected) - len(deletable)
            if deletable:
                models.BackupVersion.objects.filter(pk__in=[version.pk for version in deletable]).delete()
                messages.success(
                    request,
                    f"Deleted {len(deletable)} backup version record(s). The configurations remain in Git.",
                )
            if protected:
                messages.warning(
                    request,
                    f"Kept {protected} most-recent version(s): a device's latest backup cannot be deleted.",
                )
            return redirect(redirect_url)

        selected_rows = self._uuids(request.POST.getlist("pk"))
        if not selected_rows:
            messages.warning(request, "Select at least one device before choosing versions to delete.")
            return redirect(redirect_url)

        # The fleet table posts one row per device, so the selected PKs are BackupVersion rows; resolve
        # them to the devices they belong to.
        device_pks = list(
            models.BackupVersion.objects.for_devices_viewable_by(request.user)
            .filter(pk__in=selected_rows)
            .values_list("device_id", flat=True)
            .distinct()
        )
        if not device_pks:
            messages.warning(request, "Select at least one device before choosing versions to delete.")
            return redirect(redirect_url)

        # Redirect rather than render, so the confirmation page is a plain GET that can paginate, reload,
        # and be shared. POST-redirect-GET also keeps a browser refresh from re-submitting the selection.
        confirm_url = reverse("plugins:nautobot_golden_config:backupversion_bulk_delete")
        query = urlencode([("device", str(device_pk)) for device_pk in device_pks])
        return redirect(f"{confirm_url}?{query}")


class BackupHistoryDiffToolView(PermissionRequiredMixin, View):
    """Standalone Golden Config "Diffs" tool: pick a device (name search) or an IP and diff its backup history."""

    permission_required = ["dcim.view_device", "extras.view_gitrepository"]

    def get(self, request):
        """Render one device's backup diff (via the device picker or an IP), or the recent-changes landing."""
        form = forms.BackupHistoryDiffForm(request.GET)
        submitted = bool(request.GET.get("device") or request.GET.get("ip"))
        context = {"form": form, "device": None, "submitted": submitted}

        device = None
        if form.is_valid():
            device = form.cleaned_data.get("device")
            ip_query = (form.cleaned_data.get("ip") or "").strip()
            if device is None and ip_query:
                device = _resolve_device_by_name_or_ip(ip_query, request.user)
                if device is None:
                    # The lookup takes an exact device name or an IP, so name the accepted forms rather
                    # than assuming the user typed an IP.
                    messages.warning(
                        request,
                        f"No device found matching '{ip_query}'. Enter an exact device name or an IP address.",
                    )
        elif submitted:
            # An invalid form (e.g. ?device=<not-a-uuid>) would otherwise resolve no device and render a
            # bare "Back" button with no explanation. Say what went wrong; the template lists the errors.
            messages.warning(request, "That device lookup could not be processed; see the details below.")

        if device is not None:
            context.update(build_backup_diff_context(device, request, {"device": str(device.pk)}))
        elif not submitted:
            context.update(_backup_fleet_context(request))
        return _render_backup_diff(request, "nautobot_golden_config/backuphistorydiff.html", context)
