import asyncio
from jsonschema import ValidationError
import logging
import logging.config
import signal

from nyuki.bus import Bus
from nyuki.capabilities import Exposer, Response, resource
from nyuki.commands import get_command_kwargs
from nyuki.config import (
    get_full_config, write_conf_json, merge_configs, DEFAULT_CONF_FILE
)
from nyuki.events import Event, on_event, EventManager
from nyuki.handlers import MetaHandler
from nyuki.loop import EventLoop


log = logging.getLogger(__name__)


class Nyuki(metaclass=MetaHandler):
    """
    A lightweigh base class to build nyukis. A nyuki provides tools that shall
    help the developer with managing the following topics:
      - Bus of communication between nyukis.
      - Asynchronous events.
      - Capabilities exposure through a REST API.
    This class has been written to perform the features above in a reliable,
    single-threaded, asynchronous and concurrent-safe environment.
    The core engine of a nyuki implementation is the asyncio event loop
    (a single loop is used for all features).
    A wrapper is also provide to ease the use of asynchronous calls
    over the actions nyukis are inteded to do.
    """
    def __init__(self, **kwargs):
        kwargs = kwargs or get_command_kwargs()
        self.config_filename = kwargs.get('config', DEFAULT_CONF_FILE)
        self._config = get_full_config(**kwargs)
        logging.config.dictConfig(self._config['log'])

        self.event_loop = EventLoop(loop=asyncio.get_event_loop())
        self.event_manager = EventManager(self.event_loop)

        self._bus = self._make_bus()
        self._exposer = Exposer(self.event_loop.loop)

    @property
    def config(self):
        return self._config

    @property
    def capabilities(self):
        return self._exposer.capabilities

    @property
    def capability_exposer(self):
        return self._exposer

    @property
    def request(self):
        return self._bus.request

    @property
    def publish(self):
        return self._bus.publish

    @property
    def subscribe(self):
        return self._bus.subscribe

    def _make_bus(self):
        """
        Returns a new Bus object attribute.
        """
        return Bus(
            loop=self.event_loop,
            event_manager=self.event_manager,
            **self._config['bus'])

    def start(self):
        """
        Start the nyuki: launch the bus client and expose capabilities.
        Basically, it starts the event loop.
        """
        signal.signal(signal.SIGTERM, self.abort)
        signal.signal(signal.SIGINT, self.abort)
        self._bus.connect()
        self._exposer.expose(**self._config['api'])
        self.event_loop.start(block=True)

    def abort(self, signum, frame):
        """
        Signal handler: gracefully stop the nyuki.
        """
        log.warning("Caught signal {}".format(signum))
        self.stop()

    def stop(self, timeout=5):
        """
        Stop the nyuki. Basically, disconnect to the bus. That will eventually
        trigger a `Disconnected` event.
        """
        self._exposer.shutdown()
        self._bus.disconnect(timeout=timeout)
        self.event_loop.stop()

    def update_config(self, *new_confs):
        """
        Update the current configuration with the given list of dicts.
        """
        self._config = merge_configs(self._config, *new_confs)

    def save_config(self):
        """
        Save the current configuration dict to its JSON file.
        """
        write_conf_json(self.config, self.config_filename)

    def reload(self):
        """
        Override this to implement a reloading to your Nyuki.
        (called on PATCH /config)
        """
        self.save_config()
        self._bus.disconnect()
        logging.config.dictConfig(self._config['log'])
        self._bus = self._make_bus()
        self._bus.connect()
        self._exposer.restart(**self._config['api'])

    @resource(endpoint='/config', version='v1')
    class Configuration:

        def get(self, request):
            return Response(self._config)

        def patch(self, request):
            try:
                self.update_config(request)
            except ValidationError as error:
                return Response(body={'error': error.message}, status=400)
            else:
                self.reload()
            return Response(self._config)
