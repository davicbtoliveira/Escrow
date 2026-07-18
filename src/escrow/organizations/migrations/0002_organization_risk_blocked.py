from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("organizations", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="risk_blocked",
            field=models.BooleanField(default=False),
        )
    ]
