from aiohttp import ClientOSError
import asyncio
import json
from unittest.mock import patch, Mock
from nose.tools import assert_in, assert_raises, eq_, ok_
from slixmpp.exceptions import IqTimeout
from unittest import TestCase
from xml.sax.saxutils import escape

from nyuki.bus import _BusClient, Bus
from nyuki.events import Event, EventManager

from tests import AsyncTestCase, fake_future


class TestBusClient(TestCase):

    def setUp(self):
        self.client = _BusClient('login@localhost', 'password')

    def test_001a_init(self):
        # Ensure bus client instanciation (method 1)
        host, port = self.client._address
        eq_(host, 'localhost')
        eq_(port, 5222)

    def test_001b_init(self):
        # Ensure bus client instanciation (method 2)
        client = _BusClient('login', 'password', '127.0.0.1', 5555)
        host, port = client._address
        eq_(host, '127.0.0.1')
        eq_(port, 5555)


@patch('nyuki.bus.Bus', 'connect')
class TestBus(TestCase):

    def setUp(self):
        self.events = list()
        loop = asyncio.get_event_loop()
        self.bus = Bus(
            'login@localhost',
            'password',
            loop=loop,
            event_manager=EventManager(loop=loop))
        self.bus.event_manager.trigger = (lambda x, *y: self.events.append(x))

    def tearDown(self):
        self.events = list()

    # @patch('slixmpp.xmlstream.stanzabase.StanzaBase.send')
    # def test_001_start(self, send_mock):
    #     # When the callback _on_start is called, an event is triggered.
    #     # with patch.object(self.bus, 'client'):
    #     self.bus._on_start(None)
    #     assert_in(Event.Connected, self.events)

    def test_002_disconnect(self):
        # When the callback _on_disconnect is called, an event is triggered.
        self.bus._on_disconnect(None)
        assert_in(Event.Disconnected, self.events)

    def test_003_failure(self):
        # When the callback _on_failure is called, an event is triggered.
        self.bus._on_failure(None)
        assert_in(Event.ConnectionError, self.events)

    def test_004_on_event(self):
        # Message without a subject will trigger a ResponseReceived.
        # Also test the proper Response stanza format
        msg = self.bus.client.Message()
        msg['type'] = 'groupchat'
        msg['body'] = json.dumps({'key': 'value'})
        self.bus._on_event(msg)
        encoded_xml = escape('{"key": "value"}', entities={'"': '&quot;'})
        assert_in(encoded_xml, str(msg))
        assert_in(Event.EventReceived, self.events)

    def test_005_muc_address(self):
        muc = self.bus._muc_address('topic')
        eq_(muc, 'topic@mucs.localhost')

    @patch('slixmpp.xmlstream.stanzabase.StanzaBase.send')
    def test_006a_publish(self, send_mock):
        with patch.object(self.bus, 'subscribe') as sub_mock:
            self.bus.publish({'message': 'test'})
            send_mock.assert_called_once_with()

    def test_006b_publish_no_dict(self):
        assert_raises(TypeError, self.bus.publish, 'not a dict')

    def test_007_subscribe(self):
        with patch.object(self.bus._mucs, 'joinMUC') as mock:
            self.bus.subscribe('login')
            mock.assert_called_once_with('login@mucs.localhost', 'login')

    @patch('slixmpp.stanza.Iq.send')
    def test_008_on_register_callback(self, send_mock):
        future = asyncio.Future()
        future.set_exception(IqTimeout(None))
        m = Mock()
        m.add_done_callback = lambda f: f(future)
        send_mock.return_value = m
        self.bus._on_register(None)
        assert_in(Event.ConnectionError, self.events)


class TestBusRequest(AsyncTestCase):

    def setUp(self):
        super().setUp()
        self.bus = Bus('login@localhost', 'login', loop=self._loop)

    def test_001a_request(self):
        @fake_future
        def request(method, url, data, headers):
            eq_(method, 'get')
            eq_(url, 'url')
            eq_(data, '{"message": "text"}')
            eq_(headers, {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            })
            m = Mock()
            m.status = 200
            @fake_future
            def to_json():
                return {'response': 'text'}
            m.json = to_json
            return m

        with patch('aiohttp.request', request):
            response = self._loop.run_until_complete(
                self.bus._execute_request('url', 'get', {'message': 'text'})
                )
        eq_(response.status, 200)
        eq_(response.json, {'response': 'text'})

    def test_001b_request_no_json(self):
        @fake_future
        def request(method, url, data, headers):
            eq_(method, 'get')
            eq_(url, 'url')
            eq_(data, '{"message": "text"}')
            eq_(headers, {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            })
            m = Mock()
            m.status = 200
            @fake_future
            def to_json():
                raise ValueError
            m.json = to_json
            @fake_future
            def to_text():
                return 'something'
            m.text = to_text
            return m

        with patch('aiohttp.request', request):
            response = self._loop.run_until_complete(
                self.bus._execute_request('url', 'get', {'message': 'text'})
                )
        eq_(response.status, 200)
        eq_(response.json, None)
        eq_(response.text().result(), 'something')

    def test_001c_request_error(self):
        run = self._loop.run_until_complete
        error = {'endpoint': 'http://localhost:8080/None/api/url',
                 'error': 'ClientOSError()',
                 'data': {'message': 'text'}}
        with patch('aiohttp.request', side_effect=ClientOSError):
            with patch.object(self.bus, 'publish') as publish:
                exc = run(self.bus.request(None, 'url', 'get',
                          data={'message': 'text'}))
            publish.assert_called_once_with(error)
            ok_(isinstance(exc, ClientOSError))
