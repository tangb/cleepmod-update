#!/usr/bin/env python
# -*- coding: utf-8 -*-

from cleep.libs.internals.event import Event

class UpdateModuleInstallEvent(Event):
    """
    Update.module.install event
    """

    EVENT_NAME = 'update.module.install'
    EVENT_PROPAGATE = False
    EVENT_PARAMS = ['module', 'status']

    def __init__(self, bus, formatters_broker):
        """ 
        Constructor

        Args:
            bus (MessageBus): message bus instance
            formatters_broker (FormattersBroker): formatters broker instance
        """
        Event.__init__(self, bus, formatters_broker)
