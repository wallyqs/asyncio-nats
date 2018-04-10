# Copyright 2016-2018 The NATS Authors
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
#

import asyncio
import json
import time
from random import shuffle
from urllib.parse import urlparse

from nats.aio.errors import *
from nats.aio.utils import new_inbox
from nats.aio.nuid import NUID
from nats.protocol.parser import *

__version__ = '0.7.0'
__lang__ = 'python3'
PROTOCOL = 1

INFO_OP = b'INFO'
CONNECT_OP = b'CONNECT'
PING_OP = b'PING'
PONG_OP = b'PONG'
PUB_OP = b'PUB'
SUB_OP = b'SUB'
UNSUB_OP = b'UNSUB'
OK_OP = b'+OK'
ERR_OP = b'-ERR'
_CRLF_ = b'\r\n'
_SPC_ = b' '
_EMPTY_ = b''
EMPTY = ""

PING_PROTO = PING_OP + _CRLF_
PONG_PROTO = PONG_OP + _CRLF_
INBOX_PREFIX = bytearray(b'_INBOX.')
INBOX_PREFIX_LEN = len(INBOX_PREFIX) + 22 + 1

DEFAULT_PENDING_SIZE = 1024 * 1024
DEFAULT_BUFFER_SIZE = 32768
DEFAULT_RECONNECT_TIME_WAIT = 2 # in seconds
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_PING_INTERVAL = 120  # in seconds
DEFAULT_MAX_OUTSTANDING_PINGS = 2
DEFAULT_MAX_PAYLOAD_SIZE = 1048576
DEFAULT_MAX_FLUSHER_QUEUE_SIZE = 1024
MAX_CONTROL_LINE_SIZE = 1024

# Default Pending Limits of Subscriptions
DEFAULT_SUB_PENDING_MSGS_LIMIT  = 65536
DEFAULT_SUB_PENDING_BYTES_LIMIT = 65536 * 1024

class Subscription(object):
    def __init__(self, subject='', queue='', future=None, max_msgs=0,
                 is_async=False, cb=None, coro=None):
        self.subject = subject
        self.queue = queue
        self.future = future
        self.max_msgs = max_msgs
        self.received = 0
        self.is_async = is_async
        self.cb = cb
        self.coro = coro

        # Per subscription message processor
        self.pending_msgs_limit = None
        self.pending_bytes_limit = None
        self.pending_queue = None
        self.pending_size = 0
        self.wait_for_msgs_task = None

class Msg(object):
    __slots__ = ('subject', 'reply', 'data', 'sid')

    def __init__(self, subject='', reply='', data=b'', sid=0):
        self.subject = subject
        self.reply = reply
        self.data = data
        self.sid = sid

    def __repr__(self):
        return "<{}: subject='{}' reply='{}' data='{}...'>".format(
            self.__class__.__name__,
            self.subject,
            self.reply,
            self.data[:10].decode(),
            )

class Srv(object):
    """
    Srv is a helper data structure to hold state of a server.
    """

    def __init__(self, uri):
        self.uri = uri
        self.reconnects = 0
        self.last_attempt = None
        self.did_connect = False
        self.discovered = False


class Client(object):
    """
    Asyncio based client for NATS.
    """

    msg_class = Msg

    DISCONNECTED = 0
    CONNECTED = 1
    CLOSED = 2
    RECONNECTING = 3
    CONNECTING = 4

    def __repr__(self):
        return "<nats client v{}>".format(__version__)

    def __init__(self):
        self._loop = None
        self._current_server = None
        self._server_info = {}
        self._server_pool = []
        self._reading_task = None
        self._ping_interval_task = None
        self._pings_outstanding = 0
        self._pongs_received = 0
        self._pongs = []
        self._bare_io_reader = None
        self._io_reader = None
        self._bare_io_writer = None
        self._io_writer = None
        self._err = None
        self._error_cb = None
        self._disconnected_cb = None
        self._closed_cb = None
        self._reconnected_cb = None
        self._reconnection_task = None
        self._max_payload = DEFAULT_MAX_PAYLOAD_SIZE
        self._ssid = 0
        self._subs = {}
        self._status = Client.DISCONNECTED
        self._ps = Parser(self)
        self._pending = []
        self._pending_data_size = 0
        self._flush_queue = None
        self._flusher_task = None

        # New style request/response
        self._resp_sub = None
        self._resp_map = None
        self._resp_sub_prefix = None
        self._nuid = NUID()

        self.options = {}
        self.stats = {
            'in_msgs': 0,
            'out_msgs': 0,
            'in_bytes': 0,
            'out_bytes': 0,
            'reconnects': 0,
            'errors_received': 0,
        }

    async def connect(self,
                servers=["nats://127.0.0.1:4222"],
                io_loop=None,
                error_cb=None,
                disconnected_cb=None,
                closed_cb=None,
                reconnected_cb=None,
                name=None,
                pedantic=False,
                verbose=False,
                allow_reconnect=True,
                reconnect_time_wait=DEFAULT_RECONNECT_TIME_WAIT,
                max_reconnect_attempts=DEFAULT_MAX_RECONNECT_ATTEMPTS,
                ping_interval=DEFAULT_PING_INTERVAL,
                max_outstanding_pings=DEFAULT_MAX_OUTSTANDING_PINGS,
                dont_randomize=False,
                flusher_queue_size=DEFAULT_MAX_FLUSHER_QUEUE_SIZE,
                tls=None):
        self._setup_server_pool(servers)
        self._loop = io_loop or asyncio.get_event_loop()
        self._error_cb = error_cb
        self._closed_cb = closed_cb
        self._reconnected_cb = reconnected_cb
        self._disconnected_cb = disconnected_cb

        # Customizable options
        self.options["verbose"] = verbose
        self.options["pedantic"] = pedantic
        self.options["name"] = name
        self.options["allow_reconnect"] = allow_reconnect
        self.options["dont_randomize"] = dont_randomize
        self.options["reconnect_time_wait"] = reconnect_time_wait
        self.options["max_reconnect_attempts"] = max_reconnect_attempts
        self.options["ping_interval"] = ping_interval
        self.options["max_outstanding_pings"] = max_outstanding_pings

        if tls:
            self.options['tls'] = tls

        # Queue used to trigger flushes to the socket
        self._flush_queue = asyncio.Queue(
            maxsize=flusher_queue_size, loop=self._loop)

        if self.options["dont_randomize"] is False:
            shuffle(self._server_pool)

        while True:
            try:
                await self._select_next_server()
                await self._process_connect_init()
                self._current_server.reconnects = 0
                break
            except ErrNoServers as e:
                if self.options["max_reconnect_attempts"] < 0:
                    # Never stop reconnecting
                    continue
                self._err = e
                raise e
            except (OSError, NatsError) as e:
                self._err = e
                if self._error_cb is not None:
                    await self._error_cb(e)

                # Bail on first attempt if reconnecting is disallowed.
                if not self.options["allow_reconnect"]:
                    raise e

                await self._close(Client.DISCONNECTED, False)
                self._current_server.last_attempt = time.monotonic()
                self._current_server.reconnects += 1

    async def close(self):
        """
        Closes the socket to which we are connected and
        sets the client to be in the CLOSED state.
        No further reconnections occur once reaching this point.
        """
        await self._close(Client.CLOSED)

    async def _close(self, status, do_cbs=True):
        if self.is_closed:
            self._status = status
            return
        self._status = Client.CLOSED

        # Kick the flusher once again so it breaks
        # and avoid pending futures.
        await self._flush_pending()

        if self._reading_task is not None and not self._reading_task.cancelled():
            self._reading_task.cancel()

        if self._ping_interval_task is not None and not self._ping_interval_task.cancelled():
            self._ping_interval_task.cancel()

        if self._flusher_task is not None and not self._flusher_task.cancelled():
            self._flusher_task.cancel()

        if self._reconnection_task is not None and not self._reconnection_task.done():
            self._reconnection_task.cancel()

        # In case there is any pending data at this point, flush before disconnecting
        if self._pending_data_size > 0:
            self._io_writer.writelines(self._pending[:])
            self._pending = []
            self._pending_data_size = 0
            await self._io_writer.drain()

        # Cleanup subscriptions since not reconnecting so no need
        # to replay the subscriptions anymore.
        for i, sub in self._subs.items():
            # FIXME: Should we clear the pending queue here?
            if sub.wait_for_msgs_task is not None:
                sub.wait_for_msgs_task.cancel()
        self._subs.clear()

        if self._io_writer is not None:
            self._io_writer.close()

        if do_cbs:
            if self._disconnected_cb is not None:
                await self._disconnected_cb()
            if self._closed_cb is not None:
                await self._closed_cb()

    async def publish(self, subject, payload):
        """
        Sends a PUB command to the server on the specified subject.

          ->> PUB hello 5
          ->> MSG_PAYLOAD: world
          <<- MSG hello 2 5

        """
        if self.is_closed:
            raise ErrConnectionClosed
        payload_size = len(payload)
        if payload_size > self._max_payload:
            raise ErrMaxPayload
        await self._publish(subject, _EMPTY_, payload, payload_size)

    async def publish_request(self, subject, reply, payload):
        """
        Publishes a message tagging it with a reply subscription
        which can be used by those receiving the message to respond.

           ->> PUB hello   _INBOX.2007314fe0fcb2cdc2a2914c1 5
           ->> MSG_PAYLOAD: world
           <<- MSG hello 2 _INBOX.2007314fe0fcb2cdc2a2914c1 5

        """
        if self.is_closed:
            raise ErrConnectionClosed
        payload_size = len(payload)
        if payload_size > self._max_payload:
            raise ErrMaxPayload
        await self._publish(subject, reply.encode(), payload, payload_size)

    async def _publish(self, subject, reply, payload, payload_size):
        """
        Sends PUB command to the NATS server.
        """
        if subject == "":
            # Avoid sending messages with empty replies.
            raise ErrBadSubject

        payload_size_bytes = ("%d" % payload_size).encode()
        pub_cmd = b''.join([PUB_OP, _SPC_, subject.encode(
        ), _SPC_, reply, _SPC_, payload_size_bytes, _CRLF_, payload, _CRLF_])
        self.stats['out_msgs'] += 1
        self.stats['out_bytes'] += payload_size
        await self._send_command(pub_cmd)
        if self._flush_queue.empty():
            await self._flush_pending()

    async def subscribe(self, subject,
                  queue="",
                  cb=None,
                  future=None,
                  max_msgs=0,
                  is_async=False,
                  pending_msgs_limit=DEFAULT_SUB_PENDING_MSGS_LIMIT,
                  pending_bytes_limit=DEFAULT_SUB_PENDING_BYTES_LIMIT,
                  ):
        """
        Takes a subject string and optional queue string to send a SUB cmd,
        and a callback which to which messages (Msg) will be dispatched to
        be processed sequentially by default.
        """
        if subject == "":
            raise ErrBadSubject

        if self.is_closed:
            raise ErrConnectionClosed

        sub = Subscription(subject=subject,
                           queue=queue,
                           max_msgs=max_msgs,
                           is_async=is_async,
                           )
        if cb is not None:
            if asyncio.iscoroutinefunction(cb):
                sub.coro = cb
            elif sub.is_async:
                raise NatsError(
                    "nats: must use coroutine for async subscriptions")
            else:
                # NOTE: Consider to deprecate this eventually, it should always
                # be coroutines otherwise they could affect the single thread,
                # for now still allow to be flexible.
                sub.cb = cb

            sub.pending_msgs_limit = pending_msgs_limit
            sub.pending_bytes_limit = pending_bytes_limit
            sub.pending_queue = asyncio.Queue(
                maxsize=pending_msgs_limit,
                loop=self._loop,
                )

            # Close the delivery coroutine over the sub and error handler
            # instead of having subscription type hold over state of the conn.
            err_cb = self._error_cb

            async def wait_for_msgs():
                nonlocal sub
                nonlocal err_cb

                while True:
                    try:
                        msg = await sub.pending_queue.get()
                        sub.pending_size -= len(msg.data)

                        try:
                            # Invoke depending of type of handler.
                            if sub.coro is not None:
                                if sub.is_async:
                                    # NOTE: Deprecate this usage in a next release,
                                    # the handler implementation ought to decide
                                    # the concurrency level at which the messages
                                    # should be processed.
                                    self._loop.create_task(sub.coro(msg))
                                else:
                                    await sub.coro(msg)
                            elif sub.cb is not None:
                                if sub.is_async:
                                    raise NatsError(
                                        "nats: must use coroutine for async subscriptions")
                                else:
                                    # Schedule regular callbacks to be processed sequentially.
                                    self._loop.call_soon(sub.cb, msg)
                        except asyncio.CancelledError:
                            # In case the coroutine handler gets cancelled
                            # then stop task loop and return.
                            break
                        except Exception as e:
                            # All errors from calling a handler
                            # are async errors.
                            if err_cb is not None:
                                await err_cb(e)

                    except asyncio.CancelledError:
                        break

            # Start task for each subscription, it should be cancelled
            # on both unsubscribe and closing as well.
            sub.wait_for_msgs_task = self._loop.create_task(
                wait_for_msgs())

        elif future is not None:
            # Used to handle the single response from a request.
            sub.future = future
        else:
            raise NatsError("nats: invalid subscription type")

        self._ssid += 1
        ssid = self._ssid
        self._subs[ssid] = sub
        await self._subscribe(sub, ssid)
        return ssid

    async def subscribe_async(self, subject, **kwargs):
        """
        Sets the subcription to use a task per message to be processed.

        ..deprecated:: 7.0
          Will be removed 9.0.
        """
        kwargs["is_async"] = True
        sid = await self.subscribe(subject, **kwargs)
        return sid

    async def unsubscribe(self, ssid, max_msgs=0):
        """
        Takes a subscription sequence id and removes the subscription
        from the client, optionally after receiving more than max_msgs.
        """
        if self.is_closed:
            raise ErrConnectionClosed

        sub = None
        try:
            sub = self._subs[ssid]
        except KeyError:
            # Already unsubscribed.
            return

        # In case subscription has already received enough messages
        # then announce to the server that we are unsubscribing and
        # remove the callback locally too.
        if max_msgs == 0 or sub.received >= max_msgs:
            self._subs.pop(ssid, None)

        # Cancel task from subscription if present.
        if sub.wait_for_msgs_task is not None:
            sub.wait_for_msgs_task.cancel()

        # We will send these for all subs when we reconnect anyway,
        # so that we can suppress here.
        if not self.is_reconnecting:
            await self.auto_unsubscribe(ssid, max_msgs)

    async def _subscribe(self, sub, ssid):
        sub_cmd = b''.join([SUB_OP, _SPC_, sub.subject.encode(
        ), _SPC_, sub.queue.encode(), _SPC_, ("%d" % ssid).encode(), _CRLF_])
        await self._send_command(sub_cmd)
        await self._flush_pending()

    async def request(self, subject, payload, timeout=0.5, expected=1, cb=None):
        """
        Implements the request/response pattern via pub/sub
        using a single wildcard subscription that handles
        the responses.

        """
        # If callback given then continue to use old style.
        if cb is not None:
            next_inbox = INBOX_PREFIX[:]
            next_inbox.extend(self._nuid.next())
            inbox = next_inbox.decode()

            sid = await self.subscribe(inbox, cb=cb)
            await self.auto_unsubscribe(sid, expected)
            await self.publish_request(subject, inbox, payload)
            return sid

        if self._resp_sub_prefix is None:
            self._resp_map = {}

            # Create a prefix and single wildcard subscription once.
            self._resp_sub_prefix = INBOX_PREFIX[:]
            self._resp_sub_prefix.extend(self._nuid.next())
            self._resp_sub_prefix.extend(b'.')
            resp_mux_subject = self._resp_sub_prefix[:]
            resp_mux_subject.extend(b'*')
            sub = Subscription(subject=resp_mux_subject.decode())

            # FIXME: Allow setting pending limits for responses mux subscription.
            sub.pending_msgs_limit = DEFAULT_SUB_PENDING_MSGS_LIMIT
            sub.pending_bytes_limit = DEFAULT_SUB_PENDING_BYTES_LIMIT
            sub.pending_queue = asyncio.Queue(
                maxsize=sub.pending_msgs_limit,
                loop=self._loop,
                )

            # Single task for handling the requests
            async def wait_for_msgs():
                nonlocal sub
                while True:
                    try:
                        msg = await sub.pending_queue.get()
                        token = msg.subject[INBOX_PREFIX_LEN:]

                        try:
                            fut = self._resp_map[token]
                            fut.set_result(msg)
                            del self._resp_map[token]
                        except (asyncio.CancelledError, asyncio.InvalidStateError):
                            # Request may have timed out already so remove entry.
                            del self._resp_map[token]
                            continue
                        except KeyError:
                            # Future already handled so drop any extra
                            # responses which may have made it.
                            continue

                    except asyncio.CancelledError:
                        break

            sub.wait_for_msgs_task = self._loop.create_task(
                wait_for_msgs())

            # Store the subscription in the subscriptions map,
            # then send the protocol commands to the server.
            self._ssid += 1
            ssid = self._ssid
            self._subs[ssid] = sub
            await self._subscribe(sub, ssid)

        # Use a new NUID for the token inbox and then use the future.
        token = self._nuid.next()
        inbox = self._resp_sub_prefix[:]
        inbox.extend(token)
        future = asyncio.Future(loop=self._loop)
        self._resp_map[token.decode()] = future
        await self.publish_request(subject, inbox.decode(), payload)

        # Wait for the response or give up on timeout.
        try:
            msg = await asyncio.wait_for(future, timeout, loop=self._loop)
            return msg
        except asyncio.TimeoutError:
            future.cancel()
            raise ErrTimeout

    async def timed_request(self, subject, payload, timeout=0.5):
        """
        Implements the request/response pattern via pub/sub
        using an ephemeral subscription which will be published
        with a limited interest of 1 reply returning the response
        or raising a Timeout error.

          ->> SUB _INBOX.2007314fe0fcb2cdc2a2914c1 90
          ->> UNSUB 90 1
          ->> PUB hello _INBOX.2007314fe0fcb2cdc2a2914c1 5
          ->> MSG_PAYLOAD: world
          <<- MSG hello 2 _INBOX.2007314fe0fcb2cdc2a2914c1 5

        """
        next_inbox = INBOX_PREFIX[:]
        next_inbox.extend(self._nuid.next())
        inbox = next_inbox.decode()

        future = asyncio.Future(loop=self._loop)
        sid = await self.subscribe(inbox, future=future, max_msgs=1)
        await self.auto_unsubscribe(sid, 1)
        await self.publish_request(subject, inbox, payload)

        try:
            msg = await asyncio.wait_for(future, timeout, loop=self._loop)
            return msg
        except asyncio.TimeoutError:
            future.cancel()
            raise ErrTimeout

    async def auto_unsubscribe(self, sid, limit=1):
        """
        Sends an UNSUB command to the server.  Unsubscribe is one of the basic building
        blocks in order to be able to define request/response semantics via pub/sub
        by announcing the server limited interest a priori.
        """
        b_limit = b''
        if limit > 0:
            b_limit = ("%d" % limit).encode()
        b_sid = ("%d" % sid).encode()
        unsub_cmd = b''.join([UNSUB_OP, _SPC_, b_sid, _SPC_, b_limit, _CRLF_])
        await self._send_command(unsub_cmd)
        await self._flush_pending()

    async def flush(self, timeout=60):
        """
        Sends a pong to the server expecting a pong back ensuring
        what we have written so far has made it to the server and
        also enabling measuring of roundtrip time.
        In case a pong is not returned within the allowed timeout,
        then it will raise ErrTimeout.
        """
        if timeout <= 0:
            raise ErrBadTimeout

        if self.is_closed:
            raise ErrConnectionClosed

        future = asyncio.Future(loop=self._loop)
        try:
            await self._send_ping(future)
            await asyncio.wait_for(future, timeout, loop=self._loop)
        except asyncio.TimeoutError:
            future.cancel()
            raise ErrTimeout

    @property
    def connected_url(self):
        if self.is_connected:
            return self._current_server.uri
        else:
            return None

    @property
    def servers(self):
        servers = []
        for srv in self._server_pool:
            servers.append(srv)
        return servers

    @property
    def discovered_servers(self):
        servers = []
        for srv in self._server_pool:
            if srv.discovered:
                servers.append(srv)
        return servers

    @property
    def max_payload(self):
        """
        Returns the max payload which we received from the servers INFO
        """
        return self._max_payload

    @property
    def last_error(self):
        """
        Returns the last error which may have occured.
        """
        return self._err

    @property
    def pending_data_size(self):
        return self._pending_data_size

    @property
    def is_closed(self):
        return self._status == Client.CLOSED

    @property
    def is_reconnecting(self):
        return self._status == Client.RECONNECTING

    @property
    def is_connected(self):
        return self._status == Client.CONNECTED

    @property
    def is_connecting(self):
        return self._status == Client.CONNECTING

    async def _send_command(self, cmd, priority=False):
        if priority:
            self._pending.insert(0, cmd)
        else:
            self._pending.append(cmd)
        self._pending_data_size += len(cmd)
        if self._pending_data_size > DEFAULT_PENDING_SIZE:
            await self._flush_pending()

    async def _flush_pending(self):
        try:
            # kick the flusher!
            await self._flush_queue.put(None)

            if not self.is_connected:
                return

        except asyncio.CancelledError:
            pass

    def _setup_server_pool(self, servers):
        for server in servers:
            uri = urlparse(server)
            self._server_pool.append(Srv(uri))

    async def _select_next_server(self):
        """
        Looks up in the server pool for an available server
        and attempts to connect.
        """
        srv = None
        now = time.monotonic()
        for s in self._server_pool:
            if self.options["max_reconnect_attempts"] > 0:
                if s.reconnects > self.options["max_reconnect_attempts"]:
                    # Skip server since already tried to reconnect too many times
                    continue
            if s.did_connect and now < s.last_attempt + self.options["reconnect_time_wait"]:
                # Backoff connecting to server if we attempted recently
                await asyncio.sleep(self.options["reconnect_time_wait"], loop=self._loop)
            try:
                s.did_connect = True
                s.last_attempt = time.monotonic()
                r, w = await asyncio.open_connection(
                    s.uri.hostname,
                    s.uri.port,
                    loop=self._loop,
                    limit=DEFAULT_BUFFER_SIZE)
                srv = s

                # We keep a reference to the initial transport we used when
                # establishing the connection in case we later upgrade to TLS
                # after getting the first INFO message. This is in order to
                # prevent the GC closing the socket after we send CONNECT
                # and replace the transport.
                #
                # See https://github.com/nats-io/asyncio-nats/issues/43
                self._bare_io_reader = self._io_reader = r
                self._bare_io_writer = self._io_writer = w
                break
            except Exception as e:
                self._err = e
                if self._error_cb is not None:
                    await self._error_cb(e)
                continue

        if srv is None:
            raise ErrNoServers
        self._current_server = srv

    async def _process_err(self, err_msg):
        """
        Processes the raw error message sent by the server
        and close connection with current server.
        """
        if STALE_CONNECTION in err_msg:
            self._process_op_err(ErrStaleConnection)
            return

        if AUTHORIZATION_VIOLATION in err_msg:
            self._err = ErrAuthorization
        else:
            m = b'nats: ' + err_msg[0]
            self._err = NatsError(m.decode())

        do_cbs = False
        if not self.is_connecting:
            do_cbs = True

        # FIXME: Some errors such as 'Invalid Subscription'
        # do not cause the server to close the connection.
        # For now we handle similar as other clients and close.
        self._loop.create_task(self._close(Client.CLOSED, do_cbs))

    def _process_op_err(self, e):
        """
        Process errors which occured while reading or parsing
        the protocol. If allow_reconnect is enabled it will
        try to switch the server to which it is currently connected
        otherwise it will disconnect.
        """
        if self.is_connecting or self.is_closed or self.is_reconnecting:
            return

        if self.options["allow_reconnect"] and self.is_connected:
            self._status = Client.RECONNECTING
            self._ps.reset()

            self._reconnection_task = self._loop.create_task(self._attempt_reconnect())
        else:
            self._process_disconnect()
            self._err = e
            self._loop.create_task(self._close(Client.CLOSED, True))

    async def _attempt_reconnect(self):
        if self._reading_task is not None and not self._reading_task.cancelled():
            self._reading_task.cancel()

        if self._ping_interval_task is not None and not self._ping_interval_task.cancelled():
            self._ping_interval_task.cancel()

        if self._flusher_task is not None and not self._flusher_task.cancelled():
            self._flusher_task.cancel()

        if self._io_writer is not None:
            self._io_writer.close()

        self._err = None
        if self._disconnected_cb is not None:
            await self._disconnected_cb()

        if self.is_closed:
            return

        if self.options["dont_randomize"]:
            server = self._server_pool.pop(0)
            self._server_pool.append(server)
        else:
            shuffle(self._server_pool)

        while True:
            try:
                await self._select_next_server()
                await self._process_connect_init()
                self.stats["reconnects"] += 1
                self._current_server.reconnects = 0

                # Replay all the subscriptions in case there were some.
                for ssid, sub in self._subs.items():
                    sub_cmd = b''.join([SUB_OP, _SPC_, sub.subject.encode(
                    ), _SPC_, sub.queue.encode(), _SPC_, ("%d" % ssid).encode(), _CRLF_])
                    self._io_writer.write(sub_cmd)
                await self._io_writer.drain()

                # Flush pending data before continuing in connected status.
                # FIXME: Could use future here and wait for an error result
                # to bail earlier in case there are errors in the connection.
                await self._flush_pending()
                self._status = Client.CONNECTED
                await self.flush()
                if self._reconnected_cb is not None:
                    await self._reconnected_cb()
                break
            except ErrNoServers as e:
                if self.options["max_reconnect_attempts"] < 0:
                    # Never stop reconnecting
                    continue
                self._err = e
                await self.close()
                break
            except (OSError, NatsError, ErrTimeout) as e:
                self._err = e
                if self._error_cb is not None:
                    await self._error_cb(e)
                self._status = Client.RECONNECTING
                self._current_server.last_attempt = time.monotonic()
                self._current_server.reconnects += 1

    def _connect_command(self):
        '''
        Generates a JSON string with the params to be used
        when sending CONNECT to the server.

          ->> CONNECT {"lang": "python3"}

        '''
        options = {
            "verbose": self.options["verbose"],
            "pedantic": self.options["pedantic"],
            "lang": __lang__,
            "version": __version__,
            "protocol": PROTOCOL
        }
        if "auth_required" in self._server_info:
            if self._server_info["auth_required"]:
                # In case there is no password, then consider handle
                # sending a token instead.
                if self._current_server.uri.password is None:
                    options["auth_token"] = self._current_server.uri.username
                else:
                    options["user"] = self._current_server.uri.username
                    options["pass"] = self._current_server.uri.password
        if self.options["name"] is not None:
            options["name"] = self.options["name"]

        connect_opts = json.dumps(options, sort_keys=True)
        return b''.join([CONNECT_OP + _SPC_ + connect_opts.encode() + _CRLF_])

    async def _process_ping(self):
        """
        Process PING sent by server.
        """
        await self._send_command(PONG)
        await self._flush_pending()

    async def _process_pong(self):
        """
        Process PONG sent by server.
        """
        if len(self._pongs) > 0:
            future = self._pongs.pop(0)
            future.set_result(True)
            self._pongs_received += 1
            self._pings_outstanding -= 1

    async def _process_msg(self, sid, subject, reply, data):
        """
        Process MSG sent by server.
        """
        payload_size = len(data)
        self.stats['in_msgs'] += 1
        self.stats['in_bytes'] += payload_size

        sub = self._subs.get(sid)
        if sub is None:
            # Skip in case no subscription present.
            return

        sub.received += 1
        if sub.max_msgs > 0 and sub.received >= sub.max_msgs:
            # Enough messages so can throwaway subscription now.
            self._subs.pop(sid, None)
        msg = self._build_message(subject, reply, data)

        # Check if it is an old style request.
        if sub.future is not None:
            if sub.future.cancelled():
                # Already gave up, nothing to do.
                return
            sub.future.set_result(msg)
            return

        # Let subscription wait_for_msgs coroutine process the messages,
        # but in case sending to the subscription task would block,
        # then consider it to be an slow consumer and drop the message.
        try:
            sub.pending_size += payload_size
            if sub.pending_size >= sub.pending_bytes_limit:
                # Substract again the bytes since throwing away
                # the message so would not be pending data.
                sub.pending_size -= payload_size

                if self._error_cb is not None:
                    await self._error_cb(
                        ErrSlowConsumer(subject=subject, sid=sid))
                return
            sub.pending_queue.put_nowait(msg)
        except asyncio.QueueFull:
            if self._error_cb is not None:
                await self._error_cb(
                    ErrSlowConsumer(subject=subject, sid=sid))

    def _build_message(self, subject, reply, data):
        return self.msg_class(subject=subject.decode(), reply=reply.decode(),
                              data=data)

    def _process_disconnect(self):
        """
        Process disconnection from the server and set client status
        to DISCONNECTED.
        """
        self._status = Client.DISCONNECTED

    def _process_info(self, info):
        """
        Process INFO lines sent by the server to reconfigure client
        with latest updates from cluster to enable server discovery.
        """
        if 'connect_urls' in info:
            if info['connect_urls']:
                connect_urls = []
                for connect_url in info['connect_urls']:
                    uri = urlparse("nats://%s" % connect_url)
                    srv = Srv(uri)
                    srv.discovered = True

                    # Filter for any similar server in the server pool already.
                    should_add = True
                    for s in self._server_pool:
                        if uri.netloc == s.uri.netloc:
                            should_add = False
                    if should_add:
                        connect_urls.append(srv)

                if self.options["dont_randomize"] is not True:
                    shuffle(connect_urls)
                for srv in connect_urls:
                    self._server_pool.append(srv)

    async def _process_connect_init(self):
        """
        Process INFO received from the server and CONNECT to the server
        with authentication.  It is also responsible of setting up the
        reading and ping interval tasks from the client.
        """
        self._status = Client.CONNECTING

        # FIXME: Add readline timeout
        info_line = await self._io_reader.readline()
        if INFO_OP not in info_line:
            raise NatsError("nats: empty response from server when expecting INFO message")

        _, info = info_line.split(INFO_OP + _SPC_, 1)

        try:
            srv_info = json.loads(info.decode())
        except:
            raise NatsError("nats: info message, json parse error")

        self._process_info(srv_info)
        self._server_info = srv_info

        if 'max_payload' in self._server_info:
            self._max_payload = self._server_info["max_payload"]

        if 'tls_required' in self._server_info and self._server_info['tls_required']:
            ssl_context = self.options.get('tls')
            if not ssl_context:
                raise NatsError('nats: no ssl context provided')

            transport = self._io_writer.transport
            sock = transport.get_extra_info('socket')
            if not sock:
                # This shouldn't happen
                raise NatsError('nats: unable to get socket')

            await self._io_writer.drain()  # just in case something is left

            self._io_reader, self._io_writer = \
                await asyncio.open_connection(
                    loop=self._loop,
                    limit=DEFAULT_BUFFER_SIZE,
                    sock=sock,
                    ssl=ssl_context,
                    server_hostname=self._current_server.uri.hostname,
                )

        # Refresh state of parser upon reconnect.
        if self.is_reconnecting:
            self._ps.reset()

        connect_cmd = self._connect_command()
        self._io_writer.write(connect_cmd)
        self._io_writer.write(PING_PROTO)
        await self._io_writer.drain()

        # FIXME: Add readline timeout
        next_op = await self._io_reader.readline()
        if self.options["verbose"] and OK_OP in next_op:
            next_op = await self._io_reader.readline()

        if ERR_OP in next_op:
            err_line = next_op.decode()
            _, err_msg = err_line.split(" ", 1)
            # FIXME: Maybe handling could be more special here,
            # checking for ErrAuthorization for example.
            # await self._process_err(err_msg)
            raise NatsError("nats: " + err_msg.rstrip('\r\n'))

        if PONG_PROTO in next_op:
            self._status = Client.CONNECTED

        self._reading_task = self._loop.create_task(self._read_loop())
        self._pongs = []
        self._pings_outstanding = 0
        self._ping_interval_task = self._loop.create_task(
            self._ping_interval())

        # Task for kicking the flusher queue
        self._flusher_task = self._loop.create_task(self._flusher())

    async def _send_ping(self, future=None):
        if future is None:
            future = asyncio.Future(loop=self._loop)
        self._pongs.append(future)
        self._io_writer.write(PING_PROTO)
        await self._flush_pending()

    async def _flusher(self):
        """
        Coroutine which continuously tries to consume pending commands
        and then flushes them to the socket.
        """
        while True:
            if not self.is_connected or self.is_connecting:
                break

            try:
                await self._flush_queue.get()

                if self._pending_data_size > 0:
                    self._io_writer.writelines(self._pending[:])
                    self._pending = []
                    self._pending_data_size = 0
                    await self._io_writer.drain()
            except OSError as e:
                if self._error_cb is not None:
                    await self._error_cb(e)
                self._process_op_err(e)
                break
            except asyncio.CancelledError:
                break

    async def _ping_interval(self):
        while True:
            await asyncio.sleep(self.options["ping_interval"],
                                     loop=self._loop)
            if not self.is_connected:
                continue
            try:
                self._pings_outstanding += 1
                if self._pings_outstanding > self.options["max_outstanding_pings"]:
                    self._process_op_err(ErrStaleConnection)
                    return
                await self._send_ping()
            except asyncio.CancelledError:
                break
            # except asyncio.InvalidStateError:
            #     pass

    async def _read_loop(self):
        """
        Coroutine which gathers bytes sent by the server
        and feeds them to the protocol parser.
        In case of error while reading, it will stop running
        and its task has to be rescheduled.
        """
        while True:
            try:
                should_bail = self.is_closed or self.is_reconnecting
                if should_bail or self._io_reader is None:
                    break
                if self.is_connected and self._io_reader.at_eof():
                    if self._error_cb is not None:
                        await self._error_cb(ErrStaleConnection)
                    self._process_op_err(ErrStaleConnection)
                    break

                b = await self._io_reader.read(DEFAULT_BUFFER_SIZE)
                await self._ps.parse(b)
            except ErrProtocol:
                self._process_op_err(ErrProtocol)
                break
            except OSError as e:
                self._process_op_err(e)
                break
            except asyncio.CancelledError:
                break
            # except asyncio.InvalidStateError:
            #     pass

    def __enter__(self):
        """For when NATS client is used in a context manager"""

        return self

    def __exit__(self, *exc_info):
        """Close connection to NATS when used in a context manager"""

        self._loop.create_task(self._close(Client.CLOSED, True))
