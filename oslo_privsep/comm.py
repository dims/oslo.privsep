# Copyright 2015 Rackspace Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Serialization/Deserialization for privsep.

The wire format is a message length encoded as a simple unsigned int
in native byte order (@I in struct.pack-speak), followed by that many
bytes of UTF-8 JSON data.

"""

import json
import socket
import struct
import threading

import six

from oslo_log import log as logging
from oslo_privsep._i18n import _


LOG = logging.getLogger(__name__)

_HDRFMT = '@I'
_HDRFMT_LEN = struct.calcsize(_HDRFMT)


try:
    import greenlet

    def _get_thread_ident():
        # This returns something sensible, even if the current thread
        # isn't a greenthread
        return id(greenlet.getcurrent())

except ImportError:
    def _get_thread_ident():
        return threading.current_thread().ident


class Serializer(object):
    def __init__(self, writesock):
        self.writesock = writesock

    def send(self, msg):
        buf = json.dumps(msg, ensure_ascii=False).encode('utf-8')

        # json (the library) doesn't support push parsing and JSON
        # (the format) doesn't include length information, so we can't
        # decode without reading the entire input and blocking.  Avoid
        # that by explicitly communicating the JSON message length
        # first.
        self.writesock.sendall(struct.pack(_HDRFMT, len(buf)) + buf)

    def close(self):
        # Hilarious. `socket._socketobject.close()` doesn't actually
        # call `self._sock.close()`.  Oh well, we really wanted a half
        # close anyway.
        self.writesock.shutdown(socket.SHUT_WR)


class Deserializer(six.Iterator):
    def __init__(self, readsock):
        self.readsock = readsock

    def __iter__(self):
        return self

    def _read_n(self, n):
        """Read exactly N bytes.  Raises EOFError on premature EOF"""
        data = []
        while n > 0:
            tmp = self.readsock.recv(n)
            if not tmp:
                raise EOFError(_('Premature EOF during deserialization'))
            data.append(tmp)
            n -= len(tmp)
        return b''.join(data)

    def __next__(self):
        try:
            buflen, = struct.unpack(_HDRFMT, self._read_n(_HDRFMT_LEN))
        except EOFError:
            raise StopIteration

        return json.loads(self._read_n(buflen).decode('utf-8'))


class Future(object):
    """A very simple object to track the return of a function call"""

    def __init__(self, lock):
        self.condvar = threading.Condition(lock)
        self.error = None
        self.data = None

    def set_result(self, data):
        """Must already be holding lock used in constructor"""
        self.data = data
        self.condvar.notify()

    def set_exception(self, exc):
        """Must already be holding lock used in constructor"""
        self.error = exc
        self.condvar.notify()

    def result(self):
        """Must already be holding lock used in constructor"""
        self.condvar.wait()
        if self.error is not None:
            raise self.error
        return self.data


class ClientChannel(object):
    def __init__(self, sock):
        self.writer = Serializer(sock)
        self.lock = threading.Lock()
        self.reader_thread = threading.Thread(
            name='privsep_reader',
            target=self._reader_main,
            args=(Deserializer(sock),),
        )
        self.reader_thread.daemon = True
        self.outstanding_msgs = {}

        self.reader_thread.start()

    def _reader_main(self, reader):
        """This thread owns and demuxes the read channel"""
        for msg in reader:
            msgid, data = msg
            with self.lock:
                assert msgid in self.outstanding_msgs
                self.outstanding_msgs[msgid].set_result(data)

        # EOF.  Perhaps the privileged process exited?
        # Send an IOError to any oustanding waiting readers.  Assuming
        # the write direction is also closed, any new writes should
        # get an immediate similar error.
        LOG.debug('EOF on privsep read channel')

        exc = IOError(_('Premature eof waiting for privileged process'))
        with self.lock:
            for mbox in self.outstanding_msgs.values():
                mbox.set_exception(exc)

    def send_recv(self, msg):
        myid = _get_thread_ident()
        future = Future(self.lock)

        with self.lock:
            assert myid not in self.outstanding_msgs
            self.outstanding_msgs[myid] = future
            try:
                self.writer.send((myid, msg))

                reply = future.result()
            finally:
                del self.outstanding_msgs[myid]

        return reply

    def close(self):
        with self.lock:
            self.writer.close()

        self.reader_thread.join()


class ServerChannel(six.Iterator):
    """Server-side twin to ClientChannel"""

    def __init__(self, sock):
        self.rlock = threading.Lock()
        self.reader_iter = iter(Deserializer(sock))
        self.wlock = threading.Lock()
        self.writer = Serializer(sock)

    def __iter__(self):
        return self

    def __next__(self):
        with self.rlock:
            return next(self.reader_iter)

    def send(self, msg):
        with self.wlock:
            self.writer.send(msg)
