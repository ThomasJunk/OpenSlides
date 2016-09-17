# -*- coding: utf-8 -*-
# Generated by Django 1.9.8 on 2016-09-07 23:43
from __future__ import unicode_literals

import django.db.models.deletion
from django.db import migrations, models

from openslides.utils.autoupdate import inform_changed_data_receiver


def change_label_of_state(apps, schema_editor):
    """
    Changes the label of former state "commited a bill" to "refered to committee".
    """
    # Disconnect autoupdate. We do not want to trigger it here.
    models.signals.post_save.disconnect(dispatch_uid='inform_changed_data_receiver')

    # We get the model from the versioned app registry;
    # if we directly import it, it will be the wrong version.
    State = apps.get_model('motions', 'State')

    try:
        state = State.objects.get(name='commited a bill')
    except State.DoesNotExist:
        # State does not exists, there is nothing to change.
        pass
    else:
        state.name = 'refered to committee'
        state.action_word = 'Refer to committee'
        state.save()

    # Reconnect autoupdate.
    models.signals.post_save.connect(
        inform_changed_data_receiver,
        dispatch_uid='inform_changed_data_receiver')


def add_recommendation_labels(apps, schema_editor):
    """
    Adds recommendation labels to some of the built-in states.
    """
    # Disconnect autoupdate. We do not want to trigger it here.
    models.signals.post_save.disconnect(dispatch_uid='inform_changed_data_receiver')

    # We get the model from the versioned app registry;
    # if we directly import it, it will be the wrong version.
    State = apps.get_model('motions', 'State')

    name_label_map = {
        'accepted': 'Acceptance',
        'rejected': 'Rejection',
        'not decided': 'No decision',
        'permitted': 'Permission',
        'adjourned': 'Adjournment',
        'not concerned': 'No concernment',
        'refered to committee': 'Referral to committee',
        'rejected (not authorized)': 'Rejection (not authorized)',
    }
    for state in State.objects.all():
        if name_label_map.get(state.name):
            state.recommendation_label = name_label_map[state.name]
            state.save()


class Migration(migrations.Migration):

    dependencies = [
        ('motions', '0003_auto_20160819_0925'),
    ]

    operations = [
        migrations.AddField(
            model_name='motion',
            name='recommendation',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='motions.State'),
        ),
        migrations.AddField(
            model_name='state',
            name='recommendation_label',
            field=models.CharField(max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='motion',
            name='state',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='motions.State'),
        ),
        migrations.RunPython(
            change_label_of_state
        ),
        migrations.RunPython(
            add_recommendation_labels
        ),
    ]
