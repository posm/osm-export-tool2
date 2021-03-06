# -*- coding: utf-8 -*-
# Generated by Django 1.9 on 2016-02-26 00:17
from __future__ import unicode_literals

from django.contrib.postgres.fields.jsonb import JSONField
from django.db import migrations

class LegacyJSONField(JSONField):
    def db_type(self, connection):
        return 'json'


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0028_add_mbtiles_export_format'),
    ]

    operations = [
        migrations.AddField(
            model_name='job',
            name='metadata',
            field=LegacyJSONField(default=dict),
        ),
    ]
