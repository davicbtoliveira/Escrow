from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("agreements", "0002_protect_idempotency_checkout_token")]

    operations = [
        migrations.AddField(
            model_name="escrowagreement",
            name="funding_confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="escrowagreement",
            name="realtime_sequence",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
