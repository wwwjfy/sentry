"""
sentry.plugins.bases.notify
~~~~~~~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010-2014 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""
from __future__ import absolute_import, print_function

import logging

from django import forms

from sentry import features
from sentry.app import (
    digests,
    ratelimiter,
)
from sentry.digests.notifications import (
    event_to_record,
    unsplit_key,
)
from sentry.plugins import Notification, Plugin
from sentry.models import UserOption
from sentry.tasks.digests import deliver_digest


class NotificationConfigurationForm(forms.Form):
    pass


class BaseNotificationUserOptionsForm(forms.Form):
    def __init__(self, plugin, user, *args, **kwargs):
        self.plugin = plugin
        self.user = user
        super(BaseNotificationUserOptionsForm, self).__init__(*args, **kwargs)

    def get_title(self):
        return self.plugin.get_conf_title()

    def get_description(self):
        return ""

    def save(self):
        raise NotImplementedError


class NotificationPlugin(Plugin):
    description = ('Notify project members when a new event is seen for the first time, or when an '
                   'already resolved event has changed back to unresolved.')
    # site_conf_form = NotificationConfigurationForm
    project_conf_form = NotificationConfigurationForm

    def notify(self, notification):
        event = notification.event
        return self.notify_users(event.group, event)

    def rule_notify(self, event, futures):
        rules = []
        for future in futures:
            rules.append(future.rule)
            if not future.kwargs:
                continue
            raise NotImplementedError('The default behavior for notification de-duplication does not support args')

        if hasattr(self, 'notify_digest'):
            project = event.group.project
            if features.has('projects:digests:store', project):
                key = unsplit_key(self, event.group.project)
                if digests.add(key, event_to_record(event, rules)):
                    deliver_digest.delay(key)

            # If digest delivery is disabled, we still need to send a
            # notification -- we also need to check rate limits, since
            # ``should_notify`` skips this step if the plugin supports digests.
            if not features.has('projects:digests:deliver', project):
                if not self.__is_rate_limited(event.group, event):
                    logger = logging.getLogger('sentry.plugins.{0}'.format(self.get_conf_key()))
                    logger.info('Notification for project %r dropped due to rate limiting', project)
                    return

                notification = Notification(event=event, rules=rules)
                self.notify(notification)

        else:
            notification = Notification(event=event, rules=rules)
            self.notify(notification)

    def notify_users(self, group, event, fail_silently=False):
        raise NotImplementedError

    def notify_about_activity(self, activity):
        pass

    def get_sendable_users(self, project):
        conf_key = self.get_conf_key()

        alert_settings = dict(
            (o.user_id, int(o.value))
            for o in UserOption.objects.filter(
                project=project,
                key='%s:alert' % conf_key,
            )
        )

        disabled = set(u for u, v in alert_settings.iteritems() if v == 0)

        member_set = set(project.member_set.exclude(
            user__in=disabled,
        ).values_list('user', flat=True))

        # determine members default settings
        members_to_check = set(u for u in member_set if u not in alert_settings)
        if members_to_check:
            disabled = set(UserOption.objects.filter(
                key='subscribe_by_default',
                value='0',
                user__in=members_to_check,
            ).values_list('user', flat=True))
            member_set = filter(lambda x: x not in disabled, member_set)

        return member_set

    def __is_rate_limited(self, group, event):
        return ratelimiter.is_limited(
            project=group.project,
            key=self.get_conf_key(),
            limit=10,
        )

    def should_notify(self, group, event):
        if group.is_muted():
            return False

        # If the plugin doesn't support digests, perform rate limit checks to
        # support backwards compatibility with older plugins.
        if not hasattr(self, 'notify_digest') and self.__is_rate_limited(group, event):
            logger = logging.getLogger('sentry.plugins.{0}'.format(self.get_conf_key()))
            logger.info('Notification for project %r dropped due to rate limiting', group.project)
            return False

        return True

    def test_configuration(self, project):
        from sentry.utils.samples import create_sample_event
        event = create_sample_event(project, platform='python')
        notification = Notification(event=event)
        return self.notify(notification)

    def get_notification_doc_html(self, **kwargs):
        return ""


# Backwards-compatibility
NotifyConfigurationForm = NotificationConfigurationForm
NotifyPlugin = NotificationPlugin
