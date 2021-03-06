import asyncio
from hbmqtt.client import MQTTClient, ConnectException, ClientException
from hbmqtt.errors import NoDataException
from hbmqtt.mqtt.constants import QOS_1
import json
import logging
import re
from uuid import uuid4

from nyuki.bus import reporting
from nyuki.services import Service

from .persistence import BusPersistence, EventStatus, PersistenceError
from .utils import serialize_bus_event


log = logging.getLogger(__name__)


class MqttBus(Service):

    """
    Nyuki topics formatted as:
        - global publications:
            {nyuki_name}/publications
    """

    SERVICE = 'mqtt'
    CONF_SCHEMA = {
        'type': 'object',
        'required': ['bus'],
        'properties': {
            'bus': {
                'type': 'object',
                'required': ['name'],
                'properties': {
                    'cafile': {'type': 'string', 'minLength': 1},
                    'certfile': {'type': 'string', 'minLength': 1},
                    'host': {'type': 'string', 'minLength': 1},
                    'keyfile': {'type': 'string', 'minLength': 1},
                    'name': {'type': 'string', 'minLength': 1},
                    'port': {'type': 'integer'},
                    'persistence': {
                        'type': 'object',
                        'properties': {
                            'backend': {
                                'type': 'string',
                                'minLength': 1
                            }
                        }
                    },
                    'report_channel': {'type': 'string', 'minLength': 1},
                    'scheme': {
                        'type': 'string',
                        'enum': ['ws', 'wss', 'mqtt', 'mqtts']
                    },
                    'service': {'type': 'string', 'minLength': 1}
                },
                'additionalProperties': False
            }
        }
    }

    BASE_PUB = 'publications'

    def __init__(self, nyuki, loop=None):
        self._nyuki = nyuki
        self._loop = loop or asyncio.get_event_loop()
        self._host = None
        self._self_topic = None
        self.client = None
        self._pending = {}
        self.name = None
        self._subscriptions = {}

        # Coroutines
        self.connect_future = None
        self.listen_future = None

    @property
    def topics(self):
        return list(self._subscriptions.keys())

    def configure(self, name, scheme='mqtt', host='localhost', port=1883,
                  cafile=None, certfile=None, keyfile=None, persistence={},
                  report_channel='monitoring', service=None):
        if scheme in ['mqtts', 'wss']:
            if not cafile or not certfile or not keyfile:
                raise ValueError(
                    "secured scheme requires 'cafile', 'certfile' and 'keyfile'"
                )

        self._host = '{}://{}:{}'.format(scheme, host, port)
        self.name = name
        self._self_topic = self.publish_topic(self.name)
        self._cafile = cafile
        self._report_channel = report_channel
        self.client = MQTTClient(
            config={
                'auto_reconnect': False,
                'certfile': certfile,
                'keyfile': keyfile
            },
            loop=self._loop
        )

        # Persistence storage
        self._persistence = BusPersistence(name=name, **persistence)
        log.info('Bus persistence set to %s', self._persistence.backend)

    async def start(self):
        def cancelled(future):
            try:
                future.result()
            except asyncio.CancelledError:
                log.debug('future cancelled: %s', future)
        self.connect_future = asyncio.ensure_future(self._run())
        self.connect_future.add_done_callback(cancelled)

        try:
            await self._persistence.init()
        except PersistenceError:
            log.error(
                'Could not init persistence storage with %s',
                self._persistence.backend
            )

    async def stop(self):
        # Clean client
        if self.client is not None:
            for task in self.client.client_tasks:
                log.debug('cancelling mqtt client tasks')
                task.cancel()
            if self.client._connected_state.is_set():
                log.debug('disconnecting mqtt client')
                await self.client.disconnect()
        # Clean tasks
        if self.connect_future:
            log.debug('cancelling _run coroutine')
            self.connect_future.cancel()
        if self.listen_future:
            log.debug('cancelling _listen coroutine')
            self.listen_future.cancel()
        await self._persistence.close()
        log.info('MQTT service stopped')

    def init_reporting(self):
        """
        Initialize reporting module
        """
        reporting.init(self.name, self)

    def publish_topic(self, nyuki):
        """
        Returns the topic in which the given nyuki will send publications
        """
        return '{}/{}'.format(nyuki, self.BASE_PUB)

    def _regex_topic(self, topic):
        """
        Transform the mqtt pattern into a regex one
        """
        return r'^{}$'.format(
            topic.replace('+', '[^\/]+').replace('#', '.+')
        )

    async def replay(self, since=None, status=None):
        """
        Replay events since the given datetime (or all if None)
        """
        log.info('Replaying events')
        if since:
            log.info('    since %s', since)
        if status:
            log.info('    with status %s', status)

        events = await self._persistence.retrieve(since, status)
        for event in events:
            # Publish events one by one in the right publish time order
            await self.publish(json.loads(
                event['message']),
                event['topic'],
                event['id']
            )

    async def subscribe(self, topic, callback):
        """
        Subscribe to a topic and setup the callback
        """
        if not asyncio.iscoroutinefunction(callback):
            raise ValueError('event callback must be a coroutine')
        log.debug('MQTT subscription to %s', topic)
        await self.client.subscribe([(topic, QOS_1)])
        self._subscriptions[topic] = callback
        log.info('Callback set on regex: %s', self._regex_topic(topic))

    async def unsubscribe(self, topic):
        """
        Unsubscribe from the topic, remove callback if set
        """
        log.debug('MQTT unsubscription from %s', topic)
        await self.client.unsubscribe([topic])
        if topic in self._subscriptions:
            del self._subscriptions[topic]

    async def publish(self, data, topic=None, previous_uid=None):
        """
        Publish in given topic or default one
        """
        uid = previous_uid or str(uuid4())
        topic = topic or self._self_topic
        log.info('Publishing an event to %s', topic)
        log.debug('dump: %s', data)
        data = json.dumps(data, default=serialize_bus_event)

        # Store the event as PENDING if it is new
        if self._persistence and previous_uid is None:
            await self._persistence.store({
                'id': uid,
                'status': EventStatus.PENDING.value,
                'topic': topic,
                'message': data,
            })
            if self._persistence.memory_buffer.is_full:
                asyncio.ensure_future(self._nyuki.on_buffer_full(
                    self._persistence.memory_buffer.free_slot
                ))

        if self.client._connected_state.is_set():
            # Implies QOS_0
            await self.client.publish(topic, data.encode())
            status = EventStatus.SENT
            log.info('Event successfully sent to topic %s', topic)
        else:
            status = EventStatus.FAILED

        if self._persistence:
            await self._persistence.update(previous_uid or uid, status)

    async def _run(self):
        """
        Handle reconnection every 3 seconds
        """
        while True:
            log.info('Trying MQTT connection to %s', self._host)
            try:
                await self.client.connect(self._host, cafile=self._cafile)
            except (ConnectException, NoDataException) as exc:
                log.error(exc)
                log.info('Waiting 3 seconds to reconnect')
                await asyncio.sleep(3.0)
                continue

            # Replaying events
            log.info('Connection made with MQTT')
            if self._persistence:
                asyncio.ensure_future(self.replay(
                    status=EventStatus.not_sent()
                ))

            # Start listening
            self.listen_future = asyncio.ensure_future(self._listen())
            # Blocks until mqtt is disconnected
            await self.client._handler.wait_disconnect()
            # Clean listen_future
            self.listen_future.cancel()
            self.listen_future = None

    async def _listen(self):
        """
        Listen to events after a successful connection
        """
        while True:
            try:
                message = await self.client.deliver_message()
            except ClientException as exc:
                log.error(exc)
                break

            if message is None:
                log.info('listening loop ended')
                break

            topic = message.topic

            # Ignore own message
            if topic == self.publish_topic(self.name):
                continue

            for cb_topic, callback in self._subscriptions.items():
                if re.match(self._regex_topic(cb_topic), topic):
                    data = json.loads(message.data.decode())
                    log.debug("Event from topic '%s': %s", topic, data)
                    asyncio.ensure_future(callback(topic, data))
