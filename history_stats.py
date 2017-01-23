"""
Support for statistics about history

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.history_stats/
"""

import asyncio
import datetime
import logging
import math
import re

import homeassistant.components.history as history
import homeassistant.helpers.config_validation as cv
import pytz
import voluptuous as vol
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME, CONF_ENTITY_ID, CONF_STATE)
from homeassistant.core import callback
from homeassistant.exceptions import TemplateError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ['history']

CONF_START = 'start'
CONF_END = 'end'
CONF_DURATION = 'duration'
CONF_PERIOD_KEYS = [CONF_START, CONF_END, CONF_DURATION]

DEFAULT_NAME = 'History Statistics'
UNIT = 'h'
ICON = 'mdi:calculator'
WARMING_TIME = 3

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_ENTITY_ID): cv.entity_id,
    vol.Required(CONF_STATE): cv.slug,
    vol.Optional(CONF_START, default=None): cv.template,
    vol.Optional(CONF_END, default=None): cv.template,
    vol.Optional(CONF_DURATION, default=None): cv.template,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
})


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the History Stats sensor."""

    entity_id = config.get(CONF_ENTITY_ID)
    entity_state = config.get(CONF_STATE)
    start = config.get(CONF_START)
    end = config.get(CONF_END)
    duration = config.get(CONF_DURATION)
    name = config.get(CONF_NAME)

    if (start is None and end is None) or (start is None and duration is None) or (end is None and duration is None):
        raise _LOGGER.error('You must provide 2 of the following : start, end, duration')
    if start is not None and end is not None and duration is not None:
        raise _LOGGER.error('You have to pick exactly 2 of the following: start, end, duration')

    for template in [start, end, duration]:
        if template is not None:
            template.hass = hass
            template.template = parse_time_expr(template.template)  # Parse time aliases
            template._compiled_code = None  # Force recompilation of the template

    yield from async_add_devices(
        [HistoryStatsSensor(hass, entity_id, entity_state, start, end, duration, name)], True)
    return True


class HistoryStatsSensor(Entity):
    """Representation of a HistoryStats sensor."""

    def __init__(self, hass, entity_id, entity_state, start, end, duration, name):
        """Initialize the HistoryStats sensor."""
        self._hass = hass
        self._entity_id = entity_id
        self._entity_state = entity_state
        self._duration = duration
        self._start = start
        self._end = end
        self._name = name
        self._unit_of_measurement = UNIT

        self._init = datetime.datetime.now()
        self._period = (None, None)
        self.value = 0

        @callback
        def async_stats_sensor_state_listener(entity, old_state, new_state):
            """Called when the sensor changes state."""
            hass.async_add_job(self.async_update_ha_state, True)

        async_track_state_change(hass, entity_id, async_stats_sensor_state_listener)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.value

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._unit_of_measurement

    @property
    def should_poll(self):
        """Polling required."""
        return True

    @property
    def state_attributes(self):
        """Return the state attributes of the sensor."""
        return {
            'start': datetime.datetime.fromtimestamp(self._period[0]).strftime('%Y-%m-%d %H:%M:%S'),
            'end': datetime.datetime.fromtimestamp(self._period[1]).strftime('%Y-%m-%d %H:%M:%S'),
        }

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return ICON

    @asyncio.coroutine
    def async_update(self):
        """Get the latest data and updates the states."""

        self.update_period()
        start_timestamp, end_timestamp = self._period
        start = datetime.datetime.utcfromtimestamp(start_timestamp).replace(tzinfo=pytz.UTC)
        end = datetime.datetime.utcfromtimestamp(end_timestamp).replace(tzinfo=pytz.UTC)

        # If history functions are called immediately after init, home assistant won't start.
        # TODO : find why
        if datetime.datetime.now().timestamp() < WARMING_TIME + self._init.timestamp():
            return

        history_list = history.state_changes_during_period(start, end, str(self._entity_id))

        if self._entity_id not in history_list.keys():
            return

        # Get the first state
        last_state = history.get_state(start, self._entity_id)
        last_state = last_state is not None and last_state == self._entity_state
        last_time = start_timestamp
        elapsed = 0

        # Make calculations
        for item in history_list.get(self._entity_id):
            current_state = item.state == self._entity_state
            current_time = item.last_changed.timestamp()

            if last_state:
                elapsed += current_time - last_time

            last_state = current_state
            last_time = current_time

        self.value = round(elapsed / 3600, 2)

    def update_period(self):
        """ Parse the template values and stores a(start, end) timestamp tuple in _period"""
        start = None
        end = None

        if self._start is not None:
            try:
                start = math.floor(float(self._start.async_render()))
            except TemplateError as ex:
                handle_template_exception(ex)
            except ValueError:
                _LOGGER.error('Value for "start" cannot be converted to timestamp ')

        if self._end is not None:
            try:
                end = math.floor(float(self._end.async_render()))
            except TemplateError as ex:
                handle_template_exception(ex)
            except ValueError:
                _LOGGER.error('Value for "end" cannot be converted to timestamp ')

        if start is not None and end is not None:
            self._period = start, end
            return

        duration = None
        try:
            duration = math.floor(float(self._duration.async_render()))
        except TemplateError as ex:
            handle_template_exception(ex)
        except ValueError:
            _LOGGER.error('Value for "duration" cannot be converted to timestamp ')

        if end is None and start:
            self._period = start, start + duration
            return

        if start is None:
            self._period = end - duration, end
            return


def handle_template_exception(ex):
    if ex.args and ex.args[0].startswith("UndefinedError: 'None' has no attribute"):
        # Common during HA startup - so just a warning
        _LOGGER.warning(ex)
        return
    _LOGGER.error(ex)


def parse_time_expr(str):
    """Replace time expressions with functions accepted by templates"""

    regex = r"(now\(\)(\.replace\([a-z]+=[0-9]+\))*)"
    replacement = r"as_timestamp(\1)"

    return re.sub(regex, replacement, str.replace(
        "_THIS_YEAR_", "_THIS_MONTH_.replace(month=1)").replace(
        "_THIS_MONTH_", "_TODAY_.replace(day=1)").replace(
        "_TODAY_", "_THIS_HOUR_.replace(hour=0)").replace(
        "_THIS_HOUR_", "_THIS_MINUTE_.replace(minute=0)").replace(
        "_THIS_MINUTE_", "_NOW_.replace(second=0)").replace(
        "_NOW_", "now()"))
