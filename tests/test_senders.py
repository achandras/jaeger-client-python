# Copyright (c) 2018 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import print_function

import sys
import time
import socket
import collections

import mock
import requests
import pytest

from jaeger_client import senders, thrift
from jaeger_client import Span, SpanContext
from jaeger_client.local_agent_net import LocalAgentSender
from jaeger_client.thrift_gen.agent import Agent
from jaeger_client.thrift_gen.jaeger import ttypes
from thrift.protocol import TCompactProtocol


def test_base_sender_send_not_implemented():
    sender = senders.Sender()
    with pytest.raises(NotImplementedError):
        sender.send(1)


def test_base_sender_set_process_instantiate_jaeger_process():
    sender = senders.Sender()
    sender.set_process('service', {}, max_length=0)
    assert isinstance(sender._process, ttypes.Process)
    assert sender._process.serviceName == 'service'


def test_base_sender_spanless_flush_is_noop():
    sender = senders.Sender()
    flushed = sender.flush()
    assert flushed == 0


def test_base_sender_processless_flush_is_noop():
    sender = senders.Sender()
    sender.spans.append('foo')
    flushed = sender.flush()
    assert flushed == 0


class CustomException(Exception):
    pass


class SenderFlushTest(object):

    def span(self):
        FakeTracer = collections.namedtuple(
            'FakeTracer',
            ['ip_address', 'service_name', 'max_tag_value_length']
        )
        tracer = FakeTracer(
            ip_address='127.0.0.1',
            service_name='reporter_test',
            max_tag_value_length=sys.maxsize
        )
        ctx = SpanContext(trace_id=1, span_id=1, parent_id=None, flags=1)
        span = Span(context=ctx, tracer=tracer, operation_name='foo')
        span.start_time = time.time()
        span.end_time = span.start_time + 0.001  # 1ms
        return span

    def test_base_sender_flush_raises_exceptions(self):
        sender = senders.Sender()
        sender.set_process('service', {}, max_length=0)

        sender.spans = [self.span()]

        sender.send = mock.MagicMock(side_effect=CustomException('Failed to send batch.'))
        assert sender.span_count == 1

        try:
            sender.flush()
        except Exception as exc:
            assert isinstance(exc, CustomException)
            assert str(exc) == 'Failed to send batch.'
        else:
            assert False, "Didn't Raise"
        assert sender.span_count == 0

    def test_udp_sender_flush_reraises_exceptions(self):
        exceptions = ((CustomException, 'Failed to send batch.',
                       'Failed to submit traces to jaeger-agent: Failed to send batch.'),
                      (socket.error, 'Connection Failed',
                       'Failed to submit traces to jaeger-agent socket: Connection Failed'))
        for exception, value, expected_value in exceptions:
            sender = senders.UDPSender(host='mock', port=4242)
            sender.set_process('service', {}, max_length=0)

            sender.spans = [self.span()]

            def mock_fetch(*args):
                raise exception(value)

            assert sender.span_count == 1

            with mock.patch.object(sender._agent, 'emitBatch', mock_fetch):
                try:
                    sender.flush()
                except Exception as exc:
                    assert isinstance(exc, exception)
                    assert str(exc) == expected_value
                else:
                    assert False, "Didn't Raise"
                assert sender.span_count == 0

    def test_udp_sender_large_span_dropped_on_flush(self):
        sender = senders.UDPSender(host='mock', port=4242)
        sender.set_process('service', {'tagOne': 'someTagValue'}, max_length=0)

        span_to_send = self.span()
        span_to_filter = self.span()
        span_to_filter.set_tag('someTag', '.' * 65000)
        sender.spans = [span_to_send, span_to_filter]

        batch_store = []

        def mock_emit_batch(batch):
            batch_store.append(batch)

        with mock.patch.object(sender._agent, 'emitBatch', mock_emit_batch):
            try:
                sender.flush()
            except Exception as exc:
                assert isinstance(exc, senders.UDPSenderException)
                assert 'Cannot send span of size' in str(exc)
            else:
                assert False, "Didn't Raise"

        assert batch_store[0].spans == [thrift.make_jaeger_span(span_to_send)]

    def test_udp_sender_batches_spans_over_multiple_packets_on_flush(self):
        sender = senders.UDPSender(host='mock', port=4242)
        sender.set_process('service', {'tagOne': 'someTagValue'}, max_length=0)

        spans = [self.span() for _ in range(60)]
        for span in spans:
            span.set_tag('tag', '.' * 10000)  # Send 10 batches of 6
        sender.spans = list(spans)

        batch_store = []

        def mock_emit_batch(batch):
            batch_store.append(batch)

        with mock.patch.object(sender._agent, 'emitBatch', mock_emit_batch):
            sender.flush()

        assert len(batch_store) == 10
        c = 0
        for i in range(10):
            assert batch_store[i].spans == [
                thrift.make_jaeger_span(span) for span in spans[c:c + 6]
            ]
            c += 6


@pytest.fixture
def channel():
    channel = mock.MagicMock()
    channel._reporting_port = 4242
    channel._host = 'mock'
    return channel


def test_udp_sender_instantiate_thrift_agent(channel):

    sender = senders.UDPSender(channel)

    assert sender._agent is not None
    assert isinstance(sender._agent, Agent.Client)


def test_udp_sender_intantiate_local_agent_channel(channel):

    sender = senders.UDPSender()

    assert sender._channel is not None
    assert isinstance(sender._channel, LocalAgentSender)


def test_udp_sender_calls_agent_emitBatch_on_send(channel):

    test_data = {'foo': 'bar'}
    sender = senders.UDPSender(channel)
    sender._agent = mock.Mock()
    sender.send(test_data)
    sender._agent.emitBatch.assert_called_once_with(test_data)


def test_udp_sender_implements_thrift_protocol_factory(channel):

    sender = senders.UDPSender(channel)

    assert callable(sender.getProtocol)
    protocol = sender.getProtocol(mock.MagicMock())
    assert isinstance(protocol, TCompactProtocol.TCompactProtocol)


class HTTPSenderTest(object):

    def loaded_sender(self, *args, **kwargs):
        FakeTracer = collections.namedtuple('FakeTracer', ['ip_address', 'service_name'])
        tracer = FakeTracer(ip_address='127.0.0.1', service_name='reporter_test')
        ctx = SpanContext(trace_id=1, span_id=1, parent_id=None, flags=1)
        span = Span(context=ctx, tracer=tracer, operation_name='foo')
        span.start_time = time.time()
        span.end_time = span.start_time + 0.001
        sender = senders.HTTPSender(*args, **kwargs)
        sender.set_process('service', {}, max_length=0)
        sender.spans = [span]
        return sender

    def test_http_sender_provides_proper_request_content(self):
        sender = self.loaded_sender('some_endpoint')

        with mock.patch('requests.sessions.Session.post') as post:
            sender.flush()
            time.sleep(0.001)
            request = post.call_args[0][0]
            assert request.url == 'some_endpoint'
            assert request.headers.get('Content-Type') == 'application/x-thrift'
            assert int(request.headers.get('Content-Length')) == len(request.body)
            assert request.auth_username is None
            assert request.auth_password is None
            assert request.headers.get('Authorization') is None

    def test_http_sender_provides_auth_token(self):
        sender = self.loaded_sender('some_endpoint', auth_token='SomeAuthToken')

        with mock.patch('requests.sessions.Session.post') as post:
            sender.flush()
            time.sleep(0.001)
            request = post.call_args[0][0]
            assert request.headers.get('Authorization') == 'Bearer SomeAuthToken'

    def test_http_sender_provides_basic_auth(self):
        sender = self.loaded_sender('some_endpoint', user='SomeUser', password='SomePassword')

        with mock.patch('requests.sessions.Session.post') as post:
            sender.flush()
            time.sleep(0.001)
            request = post.call_args[0][0]
            assert request.auth_mode == 'basic'
            assert request.auth_username == 'SomeUser'
            assert request.auth_password == 'SomePassword'

    def test_http_sender_flush_reraises_exceptions(self):
        exceptions = ((CustomException('Failed to send batch.'),
                       'POST to jaeger_endpoint failed: Failed to send batch.'),
                      (socket.error('Connection Failed'),
                       'Failed to connect to jaeger_endpoint: Connection Failed'),
                      (requests.HTTPError(500, 'Server Error'),
                      'HTTP 500: Error received from Jaeger: Server Error'))

        for exception, expected_value in exceptions:
            sender = self.loaded_sender('some_endpoint')

            def mock_fetch(*args):
                raise exception

            with mock.patch('requests.sessions.Session.post', mock_fetch):
                try:
                    sender.flush()
                except Exception as exc:
                    assert isinstance(exc, type(exception))
                    assert str(exc) == expected_value
                else:
                    assert False, "Didn't Raise"
                assert sender.span_count == 0
