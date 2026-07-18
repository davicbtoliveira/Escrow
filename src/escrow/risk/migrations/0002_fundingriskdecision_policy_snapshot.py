"""Persist policy snapshots and upgrade the original v1 configuration shape."""

from __future__ import annotations

from copy import deepcopy

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def upgrade_policy_snapshots(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del schema_editor
    policy_model = apps.get_model("risk", "FundingRiskPolicy")
    decision_model = apps.get_model("risk", "FundingRiskDecision")
    default_weights = {
        "high_amount": 25,
        "customer_velocity": 40,
        "young_organization": 15,
        "high_dispute_rate": 30,
    }
    for policy in policy_model.objects.all():
        configuration = deepcopy(policy.configuration)
        if isinstance(configuration, dict) and "weights" not in configuration:
            configuration["weights"] = default_weights
            policy_model.objects.filter(pk=policy.pk).update(configuration=configuration)
        if not isinstance(configuration, dict):
            configuration = {}
        decision_model.objects.filter(policy_id=policy.pk).update(
            policy_configuration=configuration
        )


class Migration(migrations.Migration):
    dependencies = [("risk", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="fundingriskdecision",
            name="policy_configuration",
            field=models.JSONField(default=dict),
            preserve_default=False,
        ),
        migrations.RunPython(upgrade_policy_snapshots, migrations.RunPython.noop),
    ]
