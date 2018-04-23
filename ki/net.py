import asyncio
import logging
import time
from enum import Enum

from .protocol.dml import MessageManager
from .protocol.net import ServerSession as CServerSession, \
    ClientSession as CClientSession, \
    ServerDMLSession as CServerDMLSession, \
    ClientDMLSession as CClientDMLSession, \
    SessionCloseErrorCode
from .util import IDAllocator, AllocationError


def msghandler(name):
    """A decorator for binding message handlers to a function."""
    def wrapper(f):
        DMLSessionBase.handlers[name] = f
        return f
    return wrapper


class AccessLevel(Enum):
    NEW = 0
    ESTABLISHED = 1
    AUTHENTICATED = 2


class SessionBase(object):
    logger = logging.getLogger('SESSION')

    def __init__(self, transport, ensure_alive_interval=10.0):
        self.transport = transport
        self.ensure_alive_interval = ensure_alive_interval

        self._ensure_alive_task = asyncio.ensure_future(self._ensure_alive())

    async def _ensure_alive(self):
        """Ensure this session has been receiving keep alive packets periodically.

        If this session hasn't received any within the allowed time frame, the
        on_timeout() event will be triggered..
        """
        while True:
            if not self.alive:
                self.on_timeout()
                break
            await asyncio.sleep(self.ensure_alive_interval)

    def on_invalid_packet(self):
        """"Overrides `Session.on_invalid_packet()`."""
        self.logger.warning('id=%d, Got an invalid packet!' % self.id)
        self.close(SessionCloseErrorCode.INVALID_MESSAGE)

    def send_packet_data(self, data, size):
        """"Overrides `Session.send_packet_data()`."""
        self.logger.debug('id=%d, send_packet_data(%r, %d)' %
                          (self.id, data, size))

        if self.transport is not None:
            self.transport.write(data)

    def close(self, error):
        """"Overrides `Session.close()`."""
        if self._ensure_alive_task is not None:
            self._ensure_alive_task.cancel()
            self._ensure_alive_task = None

        if self._keep_alive_task is not None:
            self._keep_alive_task.cancel()
            self._keep_alive_task = None

        if self.transport is not None:
            # close() may be called more than once.
            # For this reason, we write our log message here.
            self.logger.debug('id=%d, close(%r)' % (self.id, error))

            self.transport.close()
            self.transport = None

    def on_established(self):
        """"Overrides `Session.on_established()`.

        Sets the access level to ESTABLISHED.
        """
        self.logger.debug('id=%d, on_established()' % self.id)

        self.access_level = AccessLevel.ESTABLISHED.value

    def on_timeout(self):
        """Called when this session is discovered to no longer be alive.

        This will cause the session to force-close.
        """
        self.logger.debug('id=%d, Session timed out!' % self.id)
        self.close(SessionCloseErrorCode.SESSION_DIED)


class DMLSessionBase(SessionBase):
    handlers = {}

    def on_message(self, message):
        """"Overrides `DMLSession.on_message()`."""
        self.logger.debug('id=%d, on_message(%r)' % (self.id, message.handler))

        handler_func = self.handlers.get(message.handler)
        if handler_func is not None:
            handler_func(self, message)
        else:
            self.logger.warning("id=%d, No handler found: '%s'" % (self.id, message.handler))
            # FIXME: self.close(SessionCloseErrorCode.UNHANDLED_APPLICATION_MESSAGE)

    def on_invalid_message(self, error):
        """"Overrides `DMLSession.on_invalid_message()`."""
        self.logger.warning('id=%d, Got an invalid message! (%r)' % (self.id, error))
        self.close(SessionCloseErrorCode.INVALID_MESSAGE)


class ProtocolBase(asyncio.Protocol):
    logger = logging.getLogger('PROTOCOL')

    def __init__(self):
        self.session = None

    def connection_made(self, transport):
        """"Overrides `Protocol.connection_made()`."""
        peername = transport.get_extra_info('peername')
        self.logger.debug('Connection made: %r, %d' % peername)

    def data_received(self, data):
        """"Overrides `Protocol.data_received()`.

        Passes the data off to the session for processing.
        """
        size = len(data)
        self.logger.debug('data_received(%r, %d)' % (data, size))

        if self.session is not None:
            self.session.process_data(data, size)

    def connection_lost(self, exc):
        """"Overrides `Protocol.connection_lost()`."""
        self.logger.debug('Connection lost: %r' % exc)


class ServerSessionBase(SessionBase):
    def __init__(self, server, transport,
                 keep_alive_interval=60.0, ensure_alive_interval=10.0):
        super().__init__(transport, ensure_alive_interval=ensure_alive_interval)

        self.server = server
        self.keep_alive_interval = keep_alive_interval

        self._keep_alive_task = None

    async def _keep_alive(self):
        """Sends a keep alive packet periodically."""
        while True:
            self.send_keep_alive(self.server.startup_timedelta)
            await asyncio.sleep(self.keep_alive_interval)

    def on_established(self):
        """"Overrides `SessionBase.on_established()`.

        Starts sending keep alive packets.
        """
        super().on_established()

        # Start sending keep alive packets.
        self._keep_alive_task = asyncio.ensure_future(self._keep_alive())


class ServerSession(ServerSessionBase, CServerSession):
    def __init__(self, server, transport, id,
                 keep_alive_interval=60.0, ensure_alive_interval=10.0):
        ServerSessionBase.__init__(
            self, server, transport,
            keep_alive_interval=keep_alive_interval,
            ensure_alive_interval=ensure_alive_interval)
        CServerSession.__init__(self, id)


class ServerDMLSession(DMLSessionBase, ServerSessionBase, CServerDMLSession):
    def __init__(self, server, transport, id, manager,
                 keep_alive_interval=60.0, ensure_alive_interval=10.0):
        DMLSessionBase.__init__(
            self, transport,
            ensure_alive_interval=ensure_alive_interval)
        ServerSessionBase.__init__(
            self, server, transport,
            keep_alive_interval=keep_alive_interval,
            ensure_alive_interval=ensure_alive_interval)
        CServerDMLSession.__init__(self, id, manager)


class ServerProtocol(ProtocolBase):
    def __init__(self, server):
        super().__init__()

        self.server = server

    def connection_made(self, transport):
        """"Overrides `ProtocolBase.connection_made()`.

        Creates a new session.
        """
        super().connection_made(transport)

        try:
            self.session = self.server.create_session(transport)
        except AllocationError:
            # An ID could not be allocated for a new session; refuse
            # connection.
            self.logger.warning('Failed to allocate an ID for a new session!')
            self.logger.warning('Refusing connection.')
            transport.close()
        else:
            self.session.on_connected()

    def connection_lost(self, exc):
        """"Overrides `ProtocolBase.connection_lost()`.

        Kills the session.
        """
        super().connection_lost(exc)

        if self.session is not None:
            # Free up the allocated ID.
            self.server.session_id_allocator.free(self.session.id)

            # Kill the session.
            self.session.close(SessionCloseErrorCode.SESSION_DIED)
            self.session = None

        self.server = None


class Server(object):
    PROTOCOL_CLS = ServerProtocol
    SESSION_CLS = ServerSession

    MIN_SESSION_ID = 1
    MAX_SESSION_ID = 0xFFFF

    logger = logging.getLogger('SERVER')

    def __init__(self, port):
        self.port = port

        self.startup_timestamp = time.time()

        self.session_id_allocator = IDAllocator(
            self.MIN_SESSION_ID, self.MAX_SESSION_ID)
        self.sessions = {}

    @property
    def startup_timedelta(self):
        """Returns the time that has elapsed since startup.

        This value will be in milliseconds.
        """
        return int((time.time() - self.startup_timestamp) * 1000.0)

    def run(self, event_loop):
        """Starts listening for incoming connections."""
        protocol_factory = lambda: self.PROTOCOL_CLS(self)
        coro = event_loop.create_server(protocol_factory, port=self.port)
        event_loop.run_until_complete(coro)

    def create_session(self, transport):
        """Returns a new session."""
        session_id = self.session_id_allocator.allocate()
        session = self.SESSION_CLS(self, transport, session_id)
        self.sessions[session_id] = session
        return session


class DMLServer(Server):
    SESSION_CLS = ServerDMLSession

    def __init__(self, port):
        super().__init__(port)

        self.message_manager = MessageManager()

    def create_session(self, transport):
        """Returns a new session."""
        session_id = self.session_id_allocator.allocate()
        session = self.SESSION_CLS(self, transport, session_id, self.message_manager)
        self.sessions[session_id] = session
        return session


class ClientSessionBase(SessionBase):
    def __init__(self, client, transport,
                 keep_alive_interval=10.0, ensure_alive_interval=10.0):
        super().__init__(transport, ensure_alive_interval=ensure_alive_interval)

        self.client = client
        self.keep_alive_interval = keep_alive_interval

        self._keep_alive_task = None

    async def _keep_alive(self):
        """Sends a keep alive packet periodically."""
        while True:
            self.send_keep_alive()
            await asyncio.sleep(self.keep_alive_interval)

    def on_established(self):
        """"Overrides `SessionBase.on_established()`.

        Starts sending keep alive packets.
        """
        super().on_established()

        # Start sending keep alive packets.
        self._keep_alive_task = asyncio.ensure_future(self._keep_alive())


class ClientSession(ClientSessionBase, CClientSession):
    def __init__(self, client, transport, id,
                 keep_alive_interval=60.0, ensure_alive_interval=10.0):
        ClientSessionBase.__init__(
            self, client, transport,
            keep_alive_interval=keep_alive_interval,
            ensure_alive_interval=ensure_alive_interval)
        CClientSession.__init__(self, id)


class ClientDMLSession(DMLSessionBase, ClientSessionBase, CClientDMLSession):
    def __init__(self, client, transport, id, manager,
                 keep_alive_interval=60.0, ensure_alive_interval=10.0):
        DMLSessionBase.__init__(
            self, transport,
            ensure_alive_interval=ensure_alive_interval)
        ClientSessionBase.__init__(
            self, client, transport,
            keep_alive_interval=keep_alive_interval,
            ensure_alive_interval=ensure_alive_interval)
        CClientDMLSession.__init__(self, id, manager)


class ClientProtocol(ProtocolBase):
    def __init__(self, client):
        super().__init__()

        self.client = client

    def connection_made(self, transport):
        """"Overrides `ProtocolBase.connection_made()`.

        Creates a new session.
        """
        super().connection_made(transport)

        self.session = self.client.create_session(transport)
        self.session.on_connected()

    def connection_lost(self, exc):
        """"Overrides `ProtocolBase.connection_lost()`.

        Kills the session.
        """
        super().connection_lost(exc)

        if self.session is not None:
            # Kill the session.
            self.session.close(SessionCloseErrorCode.SESSION_DIED)
            self.session = None

        self.client = None


class Client(object):
    PROTOCOL_CLS = ClientProtocol
    SESSION_CLS = ClientSession

    logger = logging.getLogger('CLIENT')

    def __init__(self, host, port):
        self.host = host
        self.port = port

        self.session = None

    def run(self, event_loop):
        """Attempts to make a connection."""
        protocol_factory = lambda: self.PROTOCOL_CLS(self)
        coro = event_loop.create_connection(
            protocol_factory, host=self.host, port=self.port)
        event_loop.run_until_complete(coro)

    def create_session(self, transport):
        """Returns a new session."""
        session = self.SESSION_CLS(self, transport, 0)
        self.session = session
        return session


class DMLClient(Client):
    SESSION_CLS = ClientDMLSession

    def __init__(self, port):
        super().__init__(port)

        self.message_manager = MessageManager()

    def create_session(self, transport):
        """Returns a new session."""
        session = self.SESSION_CLS(self, transport, 0, self.message_manager)
        self.session = session
        return session
