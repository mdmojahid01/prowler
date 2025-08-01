import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from allauth.socialaccount.models import SocialApp
from config.custom_logging import BackendLogger
from config.settings.social_login import SOCIALACCOUNT_PROVIDERS
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from django_celery_beat.models import PeriodicTask
from django_celery_results.models import TaskResult
from psqlextra.manager import PostgresManager
from psqlextra.models import PostgresPartitionedModel
from psqlextra.types import PostgresPartitioningMethod
from uuid6 import uuid7

from api.db_router import MainRouter
from api.db_utils import (
    CustomUserManager,
    FindingDeltaEnumField,
    IntegrationTypeEnumField,
    InvitationStateEnumField,
    MemberRoleEnumField,
    ProcessorTypeEnumField,
    ProviderEnumField,
    ProviderSecretTypeEnumField,
    ScanTriggerEnumField,
    SeverityEnumField,
    StateEnumField,
    StatusEnumField,
    enum_to_choices,
    generate_random_token,
    one_week_from_now,
)
from api.exceptions import ModelValidationError
from api.rls import (
    BaseSecurityConstraint,
    RowLevelSecurityConstraint,
    RowLevelSecurityProtectedModel,
    Tenant,
)
from prowler.lib.check.models import Severity

fernet = Fernet(settings.SECRETS_ENCRYPTION_KEY.encode())

# Convert Prowler Severity enum to Django TextChoices
SeverityChoices = enum_to_choices(Severity)

logger = logging.getLogger(BackendLogger.API)


class StatusChoices(models.TextChoices):
    """
    This list is based on the finding status in the Prowler CLI.

    However, it adds another state, MUTED, which is not in the CLI.
    """

    FAIL = "FAIL", _("Fail")
    PASS = "PASS", _("Pass")
    MANUAL = "MANUAL", _("Manual")


class StateChoices(models.TextChoices):
    AVAILABLE = "available", _("Available")
    SCHEDULED = "scheduled", _("Scheduled")
    EXECUTING = "executing", _("Executing")
    COMPLETED = "completed", _("Completed")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


class PermissionChoices(models.TextChoices):
    """
    Represents the different permission states that a role can have.

    Attributes:
        UNLIMITED: Indicates that the role possesses all permissions.
        LIMITED: Indicates that the role has some permissions but not all.
        NONE: Indicates that the role does not have any permissions.
    """

    UNLIMITED = "unlimited", _("Unlimited permissions")
    LIMITED = "limited", _("Limited permissions")
    NONE = "none", _("No permissions")


class ActiveProviderManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(self.active_provider_filter())

    def active_provider_filter(self):
        if self.model is Provider:
            return Q(is_deleted=False)
        elif self.model in [Finding, ComplianceOverview, ScanSummary]:
            return Q(scan__provider__is_deleted=False)
        else:
            return Q(provider__is_deleted=False)


class ActiveProviderPartitionedManager(PostgresManager, ActiveProviderManager):
    def get_queryset(self):
        return super().get_queryset().filter(self.active_provider_filter())


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=150, validators=[MinLengthValidator(3)])
    email = models.EmailField(
        max_length=254,
        unique=True,
        help_text="Case insensitive",
        error_messages={"unique": "Please check the email address and try again."},
    )
    company_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(auto_now_add=True, editable=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    objects = CustomUserManager()

    def is_member_of_tenant(self, tenant_id):
        return self.memberships.filter(tenant_id=tenant_id).exists()

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    class Meta:
        db_table = "users"

        constraints = [
            BaseSecurityConstraint(
                name="statements_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            )
        ]

    class JSONAPIMeta:
        resource_name = "users"


class Membership(models.Model):
    class RoleChoices(models.TextChoices):
        OWNER = "owner", _("Owner")
        MEMBER = "member", _("Member")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="memberships",
        related_query_name="membership",
    )
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
        related_query_name="membership",
    )
    role = MemberRoleEnumField(choices=RoleChoices.choices, default=RoleChoices.MEMBER)
    date_joined = models.DateTimeField(auto_now_add=True, editable=False)

    class Meta:
        db_table = "memberships"

        constraints = [
            models.UniqueConstraint(
                fields=("user", "tenant"),
                name="unique_resources_by_membership",
            ),
            BaseSecurityConstraint(
                name="statements_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "memberships"


class Provider(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    class ProviderChoices(models.TextChoices):
        AWS = "aws", _("AWS")
        AZURE = "azure", _("Azure")
        GCP = "gcp", _("GCP")
        KUBERNETES = "kubernetes", _("Kubernetes")
        M365 = "m365", _("M365")
        GITHUB = "github", _("GitHub")

    @staticmethod
    def validate_aws_uid(value):
        if not re.match(r"^\d{12}$", value):
            raise ModelValidationError(
                detail="AWS provider ID must be exactly 12 digits.",
                code="aws-uid",
                pointer="/data/attributes/uid",
            )

    @staticmethod
    def validate_azure_uid(value):
        try:
            val = UUID(value, version=4)
            if str(val) != value:
                raise ValueError
        except ValueError:
            raise ModelValidationError(
                detail="Azure provider ID must be a valid UUID.",
                code="azure-uid",
                pointer="/data/attributes/uid",
            )

    @staticmethod
    def validate_m365_uid(value):
        if not re.match(
            r"""^(?!-)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.(?!-)[A-Za-z0-9]"""
            r"""(?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,}$""",
            value,
        ):
            raise ModelValidationError(
                detail="M365 domain ID must be a valid domain.",
                code="m365-uid",
                pointer="/data/attributes/uid",
            )

    @staticmethod
    def validate_gcp_uid(value):
        if not re.match(r"^[a-z][a-z0-9-]{5,29}$", value):
            raise ModelValidationError(
                detail="GCP provider ID must be 6 to 30 characters, start with a letter, and contain only lowercase "
                "letters, numbers, and hyphens.",
                code="gcp-uid",
                pointer="/data/attributes/uid",
            )

    @staticmethod
    def validate_kubernetes_uid(value):
        if not re.match(
            r"^[a-zA-Z0-9][a-zA-Z0-9._@:\/-]{1,250}$",
            value,
        ):
            raise ModelValidationError(
                detail="The value must either be a valid Kubernetes UID (up to 63 characters, "
                "starting and ending with a lowercase letter or number, containing only "
                "lowercase alphanumeric characters and hyphens) or a valid AWS EKS Cluster ARN, GCP GKE Context Name or Azure AKS Cluster Name.",
                code="kubernetes-uid",
                pointer="/data/attributes/uid",
            )

    @staticmethod
    def validate_github_uid(value):
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}$", value):
            raise ModelValidationError(
                detail="GitHub provider ID must be a valid GitHub username or organization name (1-39 characters, "
                "starting with alphanumeric, containing only alphanumeric characters and hyphens).",
                code="github-uid",
                pointer="/data/attributes/uid",
            )

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    is_deleted = models.BooleanField(default=False)
    provider = ProviderEnumField(
        choices=ProviderChoices.choices, default=ProviderChoices.AWS
    )
    uid = models.CharField(
        "Unique identifier for the provider, set by the provider",
        max_length=250,
        blank=False,
        validators=[MinLengthValidator(3)],
    )
    alias = models.CharField(
        blank=True, null=True, max_length=100, validators=[MinLengthValidator(3)]
    )
    connected = models.BooleanField(null=True, blank=True)
    connection_last_checked_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    scanner_args = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()
        getattr(self, f"validate_{self.provider}_uid")(self.uid)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "providers"

        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "provider", "uid", "is_deleted"),
                name="unique_provider_uids",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "providers"


class ProviderGroup(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=255)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    providers = models.ManyToManyField(
        Provider, through="ProviderGroupMembership", related_name="provider_groups"
    )

    class Meta:
        db_table = "provider_groups"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id", "name"],
                name="unique_group_name_per_tenant",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "provider-groups"


class ProviderGroupMembership(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    provider_group = models.ForeignKey(ProviderGroup, on_delete=models.CASCADE)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    inserted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "provider_group_memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["provider_id", "provider_group"],
                name="unique_provider_group_membership",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "provider_groups-provider"


class Task(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    task_runner_task = models.OneToOneField(
        TaskResult,
        on_delete=models.CASCADE,
        related_name="task",
        related_query_name="task",
        null=True,
        blank=True,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "tasks"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

        indexes = [
            models.Index(
                fields=["id", "task_runner_task"],
                name="tasks_id_trt_id_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "tasks"


class Scan(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    class TriggerChoices(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        MANUAL = "manual", _("Manual")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(
        blank=True, null=True, max_length=100, validators=[MinLengthValidator(3)]
    )
    trigger = ScanTriggerEnumField(
        choices=TriggerChoices.choices,
    )
    state = StateEnumField(choices=StateChoices.choices, default=StateChoices.AVAILABLE)
    unique_resource_count = models.IntegerField(default=0)
    progress = models.IntegerField(default=0)
    scanner_args = models.JSONField(default=dict)
    duration = models.IntegerField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    next_scan_at = models.DateTimeField(null=True, blank=True)
    scheduler_task = models.ForeignKey(
        PeriodicTask, on_delete=models.SET_NULL, null=True, blank=True
    )
    output_location = models.CharField(blank=True, null=True, max_length=200)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.CASCADE,
        related_name="scans",
        related_query_name="scan",
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="scans",
        related_query_name="scan",
        null=True,
        blank=True,
    )
    processor = models.ForeignKey(
        "Processor",
        on_delete=models.SET_NULL,
        related_name="scans",
        related_query_name="scan",
        null=True,
        blank=True,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "scans"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

        indexes = [
            models.Index(
                fields=["provider", "state", "trigger", "scheduled_at"],
                name="scans_prov_state_trig_sche_idx",
            ),
            models.Index(
                fields=["tenant_id", "provider_id", "state", "inserted_at"],
                name="scans_prov_state_insert_idx",
            ),
            models.Index(
                fields=["tenant_id", "provider_id", "state", "-inserted_at"],
                condition=Q(state=StateChoices.COMPLETED),
                name="scans_prov_state_ins_desc_idx",
            ),
            # TODO This might replace `scans_prov_state_ins_desc_idx` completely. Review usage
            models.Index(
                fields=["tenant_id", "provider_id", "-inserted_at"],
                condition=Q(state=StateChoices.COMPLETED),
                include=["id"],
                name="scans_prov_ins_desc_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "scans"


class ResourceTag(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    key = models.TextField(blank=False)
    value = models.TextField(blank=False)

    text_search = models.GeneratedField(
        expression=SearchVector("key", weight="A", config="simple")
        + SearchVector("value", weight="B", config="simple"),
        output_field=SearchVectorField(),
        db_persist=True,
        null=True,
        editable=False,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "resource_tags"

        indexes = [
            GinIndex(fields=["text_search"], name="gin_resource_tags_search_idx"),
        ]

        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "key", "value"),
                name="unique_resource_tags_by_tenant_key_value",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]


class Resource(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    provider = models.ForeignKey(
        Provider,
        on_delete=models.CASCADE,
        related_name="resources",
        related_query_name="resource",
    )

    uid = models.TextField(
        "Unique identifier for the resource, set by the provider", blank=False
    )
    name = models.TextField("Name of the resource, as set in the provider", blank=False)
    region = models.TextField(
        "Location of the resource, as set by the provider", blank=False
    )
    service = models.TextField(
        "Service of the resource, as set by the provider", blank=False
    )
    type = models.TextField("Type of the resource, as set by the provider", blank=False)

    text_search = models.GeneratedField(
        expression=SearchVector("uid", weight="A", config="simple")
        + SearchVector("name", weight="B", config="simple")
        + SearchVector("region", weight="C", config="simple")
        + SearchVector("service", "type", weight="D", config="simple"),
        output_field=SearchVectorField(),
        db_persist=True,
        null=True,
        editable=False,
    )

    metadata = models.TextField(blank=True, null=True)
    details = models.TextField(blank=True, null=True)
    partition = models.TextField(blank=True, null=True)

    failed_findings_count = models.IntegerField(default=0)

    # Relationships
    tags = models.ManyToManyField(
        ResourceTag,
        verbose_name="Tags associated with the resource, by provider",
        through="ResourceTagMapping",
    )

    def get_tags(self, tenant_id: str) -> dict:
        return {tag.key: tag.value for tag in self.tags.filter(tenant_id=tenant_id)}

    def clear_tags(self):
        self.tags.clear()
        self.save()

    def upsert_or_delete_tags(self, tags: list[ResourceTag] | None):
        if tags is None:
            self.clear_tags()
            return

        # Add new relationships with the tenant_id field
        for tag in tags:
            ResourceTagMapping.objects.update_or_create(
                tag=tag, resource=self, tenant_id=self.tenant_id
            )

        # Save the instance
        self.save()

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "resources"

        indexes = [
            models.Index(
                fields=["uid", "region", "service", "name"],
                name="resource_uid_reg_serv_name_idx",
            ),
            models.Index(
                fields=["tenant_id", "service", "region", "type"],
                name="resource_tenant_metadata_idx",
            ),
            GinIndex(fields=["text_search"], name="gin_resources_search_idx"),
            models.Index(fields=["tenant_id", "id"], name="resources_tenant_id_idx"),
            models.Index(
                fields=["tenant_id", "provider_id"],
                name="resources_tenant_provider_idx",
            ),
            models.Index(
                fields=["tenant_id", "-failed_findings_count", "id"],
                name="resources_failed_findings_idx",
            ),
        ]

        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "provider_id", "uid"),
                name="unique_resources_by_provider",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "resources"


class ResourceTagMapping(RowLevelSecurityProtectedModel):
    # NOTE that we don't really need a primary key here,
    #      but everything is easier with django if we do
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE)
    tag = models.ForeignKey(ResourceTag, on_delete=models.CASCADE)

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "resource_tag_mappings"

        # django will automatically create indexes for:
        #   - resource_id
        #   - tag_id
        #   - tenant_id
        #   - id

        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "resource_id", "tag_id"),
                name="unique_resource_tag_mappings_by_tenant",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

        indexes = [
            models.Index(
                fields=["tenant_id", "resource_id"], name="resource_tag_tenant_idx"
            ),
        ]


class Finding(PostgresPartitionedModel, RowLevelSecurityProtectedModel):
    """
    Defines the Finding model.

    Findings uses a partitioned table to store findings. The partitions are created based on the UUIDv7 `id` field.

    Note when creating migrations, you must use `python manage.py pgmakemigrations` to create the migrations.
    """

    objects = ActiveProviderPartitionedManager()
    all_objects = models.Manager()

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["id"]

    class DeltaChoices(models.TextChoices):
        NEW = "new", _("New")
        CHANGED = "changed", _("Changed")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    first_seen_at = models.DateTimeField(editable=False, null=True)

    uid = models.CharField(max_length=300)
    delta = FindingDeltaEnumField(
        choices=DeltaChoices.choices,
        blank=True,
        null=True,
    )

    status = StatusEnumField(choices=StatusChoices)
    status_extended = models.TextField(blank=True, null=True)

    severity = SeverityEnumField(choices=SeverityChoices)

    impact = SeverityEnumField(choices=SeverityChoices)
    impact_extended = models.TextField(blank=True, null=True)

    raw_result = models.JSONField(default=dict)
    tags = models.JSONField(default=dict, null=True, blank=True)
    check_id = models.CharField(max_length=100, blank=False, null=False)
    check_metadata = models.JSONField(default=dict, null=False)
    muted = models.BooleanField(default=False, null=False)
    muted_reason = models.TextField(
        blank=True, null=True, validators=[MinLengthValidator(3)], max_length=500
    )
    compliance = models.JSONField(default=dict, null=True, blank=True)

    # Denormalize resource data for performance
    resource_regions = ArrayField(
        models.CharField(max_length=100), blank=True, null=True
    )
    resource_services = ArrayField(
        models.CharField(max_length=100),
        blank=True,
        null=True,
    )
    resource_types = ArrayField(
        models.CharField(max_length=100),
        blank=True,
        null=True,
    )

    # Relationships
    scan = models.ForeignKey(to=Scan, related_name="findings", on_delete=models.CASCADE)

    # many-to-many Resources. Relationship is defined on Resource
    resources = models.ManyToManyField(
        Resource,
        verbose_name="Resources associated with the finding",
        through="ResourceFindingMapping",
        related_name="findings",
    )

    # TODO: Add resource search
    text_search = models.GeneratedField(
        expression=SearchVector(
            "impact_extended", "status_extended", weight="A", config="simple"
        ),
        output_field=SearchVectorField(),
        db_persist=True,
        null=True,
        editable=False,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "findings"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "UPDATE", "INSERT", "DELETE"],
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s_default",
                partition_name="default",
                statements=["SELECT", "UPDATE", "INSERT", "DELETE"],
            ),
        ]

        indexes = [
            models.Index(fields=["tenant_id", "id"], name="findings_tenant_and_id_idx"),
            GinIndex(fields=["text_search"], name="gin_findings_search_idx"),
            models.Index(fields=["tenant_id", "scan_id"], name="find_tenant_scan_idx"),
            models.Index(
                fields=["tenant_id", "scan_id", "id"], name="find_tenant_scan_id_idx"
            ),
            models.Index(
                fields=["tenant_id", "id"],
                condition=Q(delta="new"),
                name="find_delta_new_idx",
            ),
            models.Index(
                fields=["tenant_id", "uid", "-inserted_at"],
                name="find_tenant_uid_inserted_idx",
            ),
            GinIndex(fields=["resource_services"], name="gin_find_service_idx"),
            GinIndex(fields=["resource_regions"], name="gin_find_region_idx"),
            GinIndex(fields=["resource_types"], name="gin_find_rtype_idx"),
            models.Index(
                fields=["tenant_id", "scan_id", "check_id"],
                name="find_tenant_scan_check_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "findings"

    def add_resources(self, resources: list[Resource] | None):
        if not resources:
            return

        self.resource_regions = self.resource_regions or []
        self.resource_services = self.resource_services or []
        self.resource_types = self.resource_types or []

        # Deduplication
        regions = set(self.resource_regions)
        services = set(self.resource_services)
        types = set(self.resource_types)

        for resource in resources:
            ResourceFindingMapping.objects.update_or_create(
                resource=resource, finding=self, tenant_id=self.tenant_id
            )
            regions.add(resource.region)
            services.add(resource.service)
            types.add(resource.type)

        self.resource_regions = list(regions)
        self.resource_services = list(services)
        self.resource_types = list(types)
        self.save()


class ResourceFindingMapping(PostgresPartitionedModel, RowLevelSecurityProtectedModel):
    """
    Defines the ResourceFindingMapping model.

    ResourceFindingMapping is used to map a Finding to a Resource.

    It follows the same partitioning strategy as the Finding model.
    """

    # NOTE that we don't really need a primary key here,
    #      but everything is easier with django if we do
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE)
    finding = models.ForeignKey(Finding, on_delete=models.CASCADE)

    class PartitioningMeta:
        method = PostgresPartitioningMethod.RANGE
        key = ["finding_id"]

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "resource_finding_mappings"
        base_manager_name = "objects"
        abstract = False

        # django will automatically create indexes for:
        #   - resource_id
        #   - finding_id
        #   - tenant_id
        #   - id

        indexes = [
            models.Index(
                fields=["tenant_id", "finding_id"],
                name="rfm_tenant_finding_idx",
            ),
            models.Index(
                fields=["tenant_id", "resource_id"],
                name="rfm_tenant_resource_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "resource_id", "finding_id"),
                name="unique_resource_finding_mappings_by_tenant",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
            RowLevelSecurityConstraint(
                "tenant_id",
                name=f"rls_on_{db_table}_default",
                partition_name="default",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]


class ProviderSecret(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    class TypeChoices(models.TextChoices):
        STATIC = "static", _("Key-value pairs")
        ROLE = "role", _("Role assumption")
        SERVICE_ACCOUNT = "service_account", _("GCP Service Account Key")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    name = models.CharField(
        blank=True, null=True, max_length=100, validators=[MinLengthValidator(3)]
    )
    secret_type = ProviderSecretTypeEnumField(choices=TypeChoices.choices)
    _secret = models.BinaryField(db_column="secret")
    provider = models.OneToOneField(
        Provider,
        on_delete=models.CASCADE,
        related_name="secret",
        related_query_name="secret",
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "provider_secrets"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "provider-secrets"

    @property
    def secret(self):
        if isinstance(self._secret, memoryview):
            encrypted_bytes = self._secret.tobytes()
        elif isinstance(self._secret, str):
            encrypted_bytes = self._secret.encode()
        else:
            encrypted_bytes = self._secret
        decrypted_data = fernet.decrypt(encrypted_bytes)
        return json.loads(decrypted_data.decode())

    @secret.setter
    def secret(self, value):
        encrypted_data = fernet.encrypt(json.dumps(value).encode())
        self._secret = encrypted_data


class Invitation(RowLevelSecurityProtectedModel):
    class State(models.TextChoices):
        PENDING = "pending", _("Invitation is pending")
        ACCEPTED = "accepted", _("Invitation was accepted by a user")
        EXPIRED = "expired", _("Invitation expired after the configured time")
        REVOKED = "revoked", _("Invitation was revoked by a user")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    email = models.EmailField(max_length=254, blank=False, null=False)
    state = InvitationStateEnumField(choices=State.choices, default=State.PENDING)
    token = models.CharField(
        max_length=14,
        unique=True,
        default=generate_random_token,
        editable=False,
        blank=False,
        null=False,
        validators=[MinLengthValidator(14)],
    )
    expires_at = models.DateTimeField(default=one_week_from_now)
    inviter = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="invitations",
        related_query_name="invitation",
        null=True,
    )

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "invitations"

        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "token", "email"),
                name="unique_tenant_token_email_by_invitation",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "invitations"


class Role(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=255)
    manage_users = models.BooleanField(default=False)
    manage_account = models.BooleanField(default=False)
    manage_billing = models.BooleanField(default=False)
    manage_providers = models.BooleanField(default=False)
    manage_integrations = models.BooleanField(default=False)
    manage_scans = models.BooleanField(default=False)
    unlimited_visibility = models.BooleanField(default=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    provider_groups = models.ManyToManyField(
        ProviderGroup, through="RoleProviderGroupRelationship", related_name="roles"
    )
    users = models.ManyToManyField(
        User, through="UserRoleRelationship", related_name="roles"
    )
    invitations = models.ManyToManyField(
        Invitation, through="InvitationRoleRelationship", related_name="roles"
    )

    # Filter permission_state
    PERMISSION_FIELDS = [
        "manage_users",
        "manage_account",
        "manage_billing",
        "manage_providers",
        "manage_integrations",
        "manage_scans",
    ]

    @property
    def permission_state(self):
        values = [getattr(self, field) for field in self.PERMISSION_FIELDS]
        if all(values):
            return PermissionChoices.UNLIMITED
        elif not any(values):
            return PermissionChoices.NONE
        else:
            return PermissionChoices.LIMITED

    @classmethod
    def filter_by_permission_state(cls, queryset, value):
        q_all_true = Q(**{field: True for field in cls.PERMISSION_FIELDS})
        q_all_false = Q(**{field: False for field in cls.PERMISSION_FIELDS})

        if value == PermissionChoices.UNLIMITED:
            return queryset.filter(q_all_true)
        elif value == PermissionChoices.NONE:
            return queryset.filter(q_all_false)
        else:
            return queryset.exclude(q_all_true | q_all_false)

    class Meta:
        db_table = "roles"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id", "name"],
                name="unique_role_per_tenant",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "roles"


class RoleProviderGroupRelationship(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    provider_group = models.ForeignKey(ProviderGroup, on_delete=models.CASCADE)
    inserted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "role_provider_group_relationship"
        constraints = [
            models.UniqueConstraint(
                fields=["role_id", "provider_group_id"],
                name="unique_role_provider_group_relationship",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "role-provider_groups"


class UserRoleRelationship(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    inserted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "role_user_relationship"
        constraints = [
            models.UniqueConstraint(
                fields=["role_id", "user_id"],
                name="unique_role_user_relationship",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "user-roles"


class InvitationRoleRelationship(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    invitation = models.ForeignKey(Invitation, on_delete=models.CASCADE)
    inserted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "role_invitation_relationship"
        constraints = [
            models.UniqueConstraint(
                fields=["role_id", "invitation_id"],
                name="unique_role_invitation_relationship",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "invitation-roles"


class ComplianceOverview(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    compliance_id = models.CharField(max_length=100, blank=False, null=False)
    framework = models.CharField(max_length=100, blank=False, null=False)
    version = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    region = models.CharField(max_length=50, blank=True)
    requirements = models.JSONField(default=dict)
    requirements_passed = models.IntegerField(default=0)
    requirements_failed = models.IntegerField(default=0)
    requirements_manual = models.IntegerField(default=0)
    total_requirements = models.IntegerField(default=0)

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="compliance_overviews",
        related_query_name="compliance_overview",
        null=True,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "compliance_overviews"

        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "scan", "compliance_id", "region"),
                name="unique_tenant_scan_region_compliance_by_compliance_overview",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "DELETE"],
            ),
        ]
        indexes = [
            models.Index(fields=["compliance_id"], name="comp_ov_cp_id_idx"),
            models.Index(fields=["requirements_failed"], name="comp_ov_req_fail_idx"),
            models.Index(
                fields=["compliance_id", "requirements_failed"],
                name="comp_ov_cp_id_req_fail_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "compliance-overviews"


class ComplianceRequirementOverview(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    compliance_id = models.TextField(blank=False)
    framework = models.TextField(blank=False)
    version = models.TextField(blank=True)
    description = models.TextField(blank=True)
    region = models.TextField(blank=False)

    requirement_id = models.TextField(blank=False)
    requirement_status = StatusEnumField(choices=StatusChoices)
    passed_checks = models.IntegerField(default=0)
    failed_checks = models.IntegerField(default=0)
    total_checks = models.IntegerField(default=0)

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="compliance_requirements_overviews",
        related_query_name="compliance_requirements_overview",
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "compliance_requirements_overviews"

        constraints = [
            models.UniqueConstraint(
                fields=(
                    "tenant_id",
                    "scan_id",
                    "compliance_id",
                    "requirement_id",
                    "region",
                ),
                name="unique_tenant_compliance_requirement_overview",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "DELETE"],
            ),
        ]
        indexes = [
            models.Index(fields=["tenant_id", "scan_id"], name="cro_tenant_scan_idx"),
            models.Index(
                fields=["tenant_id", "scan_id", "compliance_id"],
                name="cro_scan_comp_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "compliance_id", "region"],
                name="cro_scan_comp_reg_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "compliance_id", "requirement_id"],
                name="cro_scan_comp_req_idx",
            ),
            models.Index(
                fields=[
                    "tenant_id",
                    "scan_id",
                    "compliance_id",
                    "requirement_id",
                    "region",
                ],
                name="cro_scan_comp_req_reg_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "compliance-requirements-overviews"


class ScanSummary(RowLevelSecurityProtectedModel):
    objects = ActiveProviderManager()
    all_objects = models.Manager()

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    check_id = models.CharField(max_length=100, blank=False, null=False)
    service = models.TextField(blank=False)
    severity = SeverityEnumField(choices=SeverityChoices)
    region = models.TextField(blank=False)
    _pass = models.IntegerField(db_column="pass", default=0)
    fail = models.IntegerField(default=0)
    muted = models.IntegerField(default=0)
    total = models.IntegerField(default=0)
    new = models.IntegerField(default=0)
    changed = models.IntegerField(default=0)
    unchanged = models.IntegerField(default=0)

    fail_new = models.IntegerField(default=0)
    fail_changed = models.IntegerField(default=0)
    pass_new = models.IntegerField(default=0)
    pass_changed = models.IntegerField(default=0)
    muted_new = models.IntegerField(default=0)
    muted_changed = models.IntegerField(default=0)

    scan = models.ForeignKey(
        Scan,
        on_delete=models.CASCADE,
        related_name="aggregations",
        related_query_name="aggregation",
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "scan_summaries"

        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "scan", "check_id", "service", "severity", "region"),
                name="unique_scan_summary",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant_id", "scan_id"],
                name="scan_summaries_tenant_scan_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "service"],
                name="ss_tenant_scan_service_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "severity"],
                name="ss_tenant_scan_severity_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "scan-summaries"


class Integration(RowLevelSecurityProtectedModel):
    class IntegrationChoices(models.TextChoices):
        S3 = "amazon_s3", _("Amazon S3")
        AWS_SECURITY_HUB = "aws_security_hub", _("AWS Security Hub")
        JIRA = "jira", _("JIRA")
        SLACK = "slack", _("Slack")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    enabled = models.BooleanField(default=False)
    connected = models.BooleanField(null=True, blank=True)
    connection_last_checked_at = models.DateTimeField(null=True, blank=True)
    integration_type = IntegrationTypeEnumField(choices=IntegrationChoices.choices)
    configuration = models.JSONField(default=dict)
    _credentials = models.BinaryField(db_column="credentials")

    providers = models.ManyToManyField(
        Provider,
        related_name="integrations",
        through="IntegrationProviderRelationship",
        blank=True,
    )

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "integrations"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]

    class JSONAPIMeta:
        resource_name = "integrations"

    @property
    def credentials(self):
        if isinstance(self._credentials, memoryview):
            encrypted_bytes = self._credentials.tobytes()
        elif isinstance(self._credentials, str):
            encrypted_bytes = self._credentials.encode()
        else:
            encrypted_bytes = self._credentials
        decrypted_data = fernet.decrypt(encrypted_bytes)
        return json.loads(decrypted_data.decode())

    @credentials.setter
    def credentials(self, value):
        encrypted_data = fernet.encrypt(json.dumps(value).encode())
        self._credentials = encrypted_data


class IntegrationProviderRelationship(RowLevelSecurityProtectedModel):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    integration = models.ForeignKey(Integration, on_delete=models.CASCADE)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    inserted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "integration_provider_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["integration_id", "provider_id"],
                name="unique_integration_provider_rel",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]


class SAMLToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    expires_at = models.DateTimeField(editable=False)
    token = models.JSONField(unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        db_table = "saml_tokens"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=15)
        super().save(*args, **kwargs)

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class SAMLDomainIndex(models.Model):
    """
    Public index of SAML domains. No RLS. Used for fast lookup in SAML login flow.
    """

    email_domain = models.CharField(max_length=254, unique=True)
    tenant = models.ForeignKey("Tenant", on_delete=models.CASCADE)

    class Meta:
        db_table = "saml_domain_index"

        constraints = [
            models.UniqueConstraint(
                fields=("email_domain", "tenant"),
                name="unique_resources_by_email_domain",
            ),
            BaseSecurityConstraint(
                name="statements_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]


class SAMLConfiguration(RowLevelSecurityProtectedModel):
    """
    Stores per-tenant SAML settings, including email domain and IdP metadata.
    Automatically syncs to a SocialApp instance on save.

    Note:
    This model exists to provide a tenant-aware abstraction over SAML configuration.
    It supports row-level security, custom validation, and metadata parsing, enabling
    Prowler to expose a clean API and admin interface for managing SAML integrations.

    Although Django Allauth uses the SocialApp model to store provider configuration,
    it is not designed for multi-tenant use. SocialApp lacks support for tenant scoping,
    email domain mapping, and structured metadata handling.

    By managing SAMLConfiguration separately, we ensure:
        - Strong isolation between tenants via RLS.
        - Ownership of raw IdP metadata and its validation.
        - An explicit link between SAML config and business-level identifiers (e.g. email domain).
        - Programmatic transformation into the SocialApp format used by Allauth.

    In short, this model acts as a secure and user-friendly layer over Allauth's lower-level primitives.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    email_domain = models.CharField(
        max_length=254,
        unique=True,
        help_text="Email domain used to identify the tenant, e.g. prowlerdemo.com",
    )
    metadata_xml = models.TextField(
        help_text="Raw IdP metadata XML to configure SingleSignOnService, certificates, etc."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class JSONAPIMeta:
        resource_name = "saml-configurations"

    class Meta:
        db_table = "saml_configurations"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
            # 1 config per tenant
            models.UniqueConstraint(
                fields=["tenant"],
                name="unique_samlconfig_per_tenant",
            ),
        ]

    def clean(self, old_email_domain=None, is_create=False):
        # Domain must not contain @
        if "@" in self.email_domain:
            raise ValidationError({"email_domain": "Domain must not contain @"})

        # Enforce at most one config per tenant
        qs = SAMLConfiguration.objects.filter(tenant=self.tenant)
        # Exclude ourselves in case of update
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        if qs.exists():
            raise ValidationError(
                {"tenant": "A SAML configuration already exists for this tenant."}
            )

        # The email domain must be unique in the entire system
        qs = SAMLConfiguration.objects.using(MainRouter.admin_db).filter(
            email_domain__iexact=self.email_domain
        )
        if qs.exists() and old_email_domain != self.email_domain:
            raise ValidationError(
                {"tenant": "There is a problem with your email domain."}
            )

        # The entityID must be unique in the system
        idp_settings = self._parsed_metadata
        entity_id = idp_settings.get("entity_id")

        if entity_id:
            # Find any SocialApp with this entityID
            q = SocialApp.objects.filter(provider="saml", provider_id=entity_id)

            # If updating, exclude our own SocialApp from the check
            if not is_create:
                q = q.exclude(client_id=old_email_domain)
            else:
                q = q.exclude(client_id=self.email_domain)

            if q.exists():
                raise ValidationError(
                    {"metadata_xml": "There is a problem with your metadata."}
                )

    def save(self, *args, **kwargs):
        self.email_domain = self.email_domain.strip().lower()
        is_create = not SAMLConfiguration.objects.filter(pk=self.pk).exists()

        if not is_create:
            old = SAMLConfiguration.objects.get(pk=self.pk)
            old_email_domain = old.email_domain
            old_metadata_xml = old.metadata_xml
        else:
            old_email_domain = None
            old_metadata_xml = None

        self._parsed_metadata = self._parse_metadata()
        self.clean(old_email_domain, is_create)
        super().save(*args, **kwargs)

        if is_create or (
            old_email_domain != self.email_domain
            or old_metadata_xml != self.metadata_xml
        ):
            self._sync_social_app(old_email_domain)

        # Sync the public index
        if not is_create and old_email_domain and old_email_domain != self.email_domain:
            SAMLDomainIndex.objects.filter(email_domain=old_email_domain).delete()

        # Create/update the new domain index
        SAMLDomainIndex.objects.update_or_create(
            email_domain=self.email_domain, defaults={"tenant": self.tenant}
        )

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)

        SocialApp.objects.filter(provider="saml", client_id=self.email_domain).delete()
        SAMLDomainIndex.objects.filter(email_domain=self.email_domain).delete()

    def _parse_metadata(self):
        """
        Parse the raw IdP metadata XML and extract:
            - entity_id
            - sso_url
            - slo_url (may be None)
            - x509cert (required)
        """
        ns = {
            "md": "urn:oasis:names:tc:SAML:2.0:metadata",
            "ds": "http://www.w3.org/2000/09/xmldsig#",
        }
        try:
            root = ET.fromstring(self.metadata_xml)
        except ET.ParseError as e:
            raise ValidationError({"metadata_xml": f"Invalid XML: {e}"})

        # Entity ID
        entity_id = root.attrib.get("entityID")
        if not entity_id:
            raise ValidationError({"metadata_xml": "Missing entityID in metadata."})

        # SSO endpoint (must exist)
        sso = root.find(".//md:IDPSSODescriptor/md:SingleSignOnService", ns)
        if sso is None or "Location" not in sso.attrib:
            raise ValidationError(
                {"metadata_xml": "Missing SingleSignOnService in metadata."}
            )
        sso_url = sso.attrib["Location"]

        # SLO endpoint (optional)
        slo = root.find(".//md:IDPSSODescriptor/md:SingleLogoutService", ns)
        slo_url = slo.attrib.get("Location") if slo is not None else None

        # X.509 certificate (required)
        cert = root.find(
            './/md:KeyDescriptor[@use="signing"]/ds:KeyInfo/ds:X509Data/ds:X509Certificate',
            ns,
        )
        if cert is None or not cert.text or not cert.text.strip():
            raise ValidationError(
                {
                    "metadata_xml": 'Metadata must include a <ds:X509Certificate> under <KeyDescriptor use="signing">.'
                }
            )
        x509cert = cert.text.strip()

        return {
            "entity_id": entity_id,
            "sso_url": sso_url,
            "slo_url": slo_url,
            "x509cert": x509cert,
        }

    def _sync_social_app(self, previous_email_domain=None):
        """
        Create or update the corresponding SocialApp based on email_domain.
        If the domain changed, update the matching SocialApp.
        """
        settings_dict = SOCIALACCOUNT_PROVIDERS["saml"].copy()
        settings_dict["idp"] = self._parsed_metadata

        current_site = Site.objects.get(id=settings.SITE_ID)

        social_app_qs = SocialApp.objects.filter(
            provider="saml", client_id=previous_email_domain or self.email_domain
        )

        client_id = self.email_domain[:191]
        name = f"SAML-{self.email_domain}"[:40]

        if social_app_qs.exists():
            social_app = social_app_qs.first()
            social_app.client_id = client_id
            social_app.name = name
            social_app.settings = settings_dict
            social_app.provider_id = self._parsed_metadata["entity_id"]
            social_app.save()
            social_app.sites.set([current_site])
        else:
            social_app = SocialApp.objects.create(
                provider="saml",
                client_id=client_id,
                name=name,
                settings=settings_dict,
                provider_id=self._parsed_metadata["entity_id"],
            )
            social_app.sites.set([current_site])


class ResourceScanSummary(RowLevelSecurityProtectedModel):
    scan_id = models.UUIDField(default=uuid7, db_index=True)
    resource_id = models.UUIDField(default=uuid4, db_index=True)
    service = models.CharField(max_length=100)
    region = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=100)

    class Meta:
        db_table = "resource_scan_summaries"
        unique_together = (("tenant_id", "scan_id", "resource_id"),)

        indexes = [
            # Single-dimension lookups:
            models.Index(
                fields=["tenant_id", "scan_id", "service"],
                name="rss_tenant_scan_svc_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "region"],
                name="rss_tenant_scan_reg_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "resource_type"],
                name="rss_tenant_scan_type_idx",
            ),
            # Two-dimension cross-filters:
            models.Index(
                fields=["tenant_id", "scan_id", "region", "service"],
                name="rss_tenant_scan_reg_svc_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "service", "resource_type"],
                name="rss_tenant_scan_svc_type_idx",
            ),
            models.Index(
                fields=["tenant_id", "scan_id", "region", "resource_type"],
                name="rss_tenant_scan_reg_type_idx",
            ),
        ]

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]


class LighthouseConfiguration(RowLevelSecurityProtectedModel):
    """
    Stores configuration and API keys for LLM services.
    """

    class ModelChoices(models.TextChoices):
        GPT_4O_2024_11_20 = "gpt-4o-2024-11-20", _("GPT-4o v2024-11-20")
        GPT_4O_2024_08_06 = "gpt-4o-2024-08-06", _("GPT-4o v2024-08-06")
        GPT_4O_2024_05_13 = "gpt-4o-2024-05-13", _("GPT-4o v2024-05-13")
        GPT_4O = "gpt-4o", _("GPT-4o Default")
        GPT_4O_MINI_2024_07_18 = "gpt-4o-mini-2024-07-18", _("GPT-4o Mini v2024-07-18")
        GPT_4O_MINI = "gpt-4o-mini", _("GPT-4o Mini Default")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)

    name = models.CharField(
        max_length=100,
        validators=[MinLengthValidator(3)],
        blank=False,
        null=False,
        help_text="Name of the configuration",
    )
    api_key = models.BinaryField(
        blank=False, null=False, help_text="Encrypted API key for the LLM service"
    )
    model = models.CharField(
        max_length=50,
        choices=ModelChoices.choices,
        blank=False,
        null=False,
        default=ModelChoices.GPT_4O_2024_08_06,
        help_text="Must be one of the supported model names",
    )
    temperature = models.FloatField(default=0, help_text="Must be between 0 and 1")
    max_tokens = models.IntegerField(
        default=4000, help_text="Must be between 500 and 5000"
    )
    business_context = models.TextField(
        blank=True,
        null=False,
        default="",
        help_text="Additional business context for this AI model configuration",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()

        # Validate temperature
        if not 0 <= self.temperature <= 1:
            raise ModelValidationError(
                detail="Temperature must be between 0 and 1",
                code="invalid_temperature",
                pointer="/data/attributes/temperature",
            )

        # Validate max_tokens
        if not 500 <= self.max_tokens <= 5000:
            raise ModelValidationError(
                detail="Max tokens must be between 500 and 5000",
                code="invalid_max_tokens",
                pointer="/data/attributes/max_tokens",
            )

    @property
    def api_key_decoded(self):
        """Return the decrypted API key, or None if unavailable or invalid."""
        if not self.api_key:
            return None

        try:
            decrypted_key = fernet.decrypt(bytes(self.api_key))
            return decrypted_key.decode()

        except InvalidToken:
            logger.warning("Invalid token while decrypting API key.")
        except Exception as e:
            logger.exception("Unexpected error while decrypting API key: %s", e)

    @api_key_decoded.setter
    def api_key_decoded(self, value):
        """Store the encrypted API key."""
        if not value:
            raise ModelValidationError(
                detail="API key is required",
                code="invalid_api_key",
                pointer="/data/attributes/api_key",
            )

        # Validate OpenAI API key format
        openai_key_pattern = r"^sk-[\w-]+T3BlbkFJ[\w-]+$"
        if not re.match(openai_key_pattern, value):
            raise ModelValidationError(
                detail="Invalid OpenAI API key format.",
                code="invalid_api_key",
                pointer="/data/attributes/api_key",
            )
        self.api_key = fernet.encrypt(value.encode())

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "lighthouse_configurations"

        constraints = [
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
            # Add unique constraint for name within a tenant
            models.UniqueConstraint(
                fields=["tenant_id"], name="unique_lighthouse_config_per_tenant"
            ),
        ]

    class JSONAPIMeta:
        resource_name = "lighthouse-configurations"


class Processor(RowLevelSecurityProtectedModel):
    class ProcessorChoices(models.TextChoices):
        MUTELIST = "mutelist", _("Mutelist")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    inserted_at = models.DateTimeField(auto_now_add=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, editable=False)
    processor_type = ProcessorTypeEnumField(choices=ProcessorChoices.choices)
    configuration = models.JSONField(default=dict)

    class Meta(RowLevelSecurityProtectedModel.Meta):
        db_table = "processors"

        constraints = [
            models.UniqueConstraint(
                fields=("tenant_id", "processor_type"),
                name="unique_processor_types_tenant",
            ),
            RowLevelSecurityConstraint(
                field="tenant_id",
                name="rls_on_%(class)s",
                statements=["SELECT", "INSERT", "UPDATE", "DELETE"],
            ),
        ]
        indexes = [
            models.Index(
                fields=["tenant_id", "id"],
                name="processor_tenant_id_idx",
            ),
            models.Index(
                fields=["tenant_id", "processor_type"],
                name="processor_tenant_type_idx",
            ),
        ]

    class JSONAPIMeta:
        resource_name = "processors"
