import logging
import asyncio

import grpc

import etcd3.etcdrpc as etcdrpc
import etcd3.events as events
import etcd3.exceptions as exceptions
import etcd3.utils as utils


_log = logging.getLogger(__name__)


def create_task(coro):
    asyncio.get_event_loop().create_task(coro)


class Watch(object):

    def __init__(self, watch_id, iterator=None, etcd_client=None):
        self.watch_id = watch_id
        self.etcd_client = etcd_client
        self.iterator = iterator

    async def cancel(self):
        await self.etcd_client.cancel_watch(self.watch_id)

    def iterator(self):
        if self.iterator is not None:
            return self.iterator

        raise ValueError('Undefined iterator')


class Watcher(object):

    def __init__(self, watchstub, timeout=None, call_credentials=None,
                 metadata=None):
        self.timeout = timeout
        self._watch_stub = watchstub
        self._credentials = call_credentials
        self._metadata = metadata

        self._lock = asyncio.Lock()
        self._request_queue = asyncio.Queue(maxsize=10)
        self._callbacks = {}
        self._callback_thread = None
        self._new_watch_cond = asyncio.Condition(lock=self._lock)
        self._new_watch = None

    async def add_callback(self, key, callback, range_end=None,
                           start_revision=None, progress_notify=False,
                           filters=None, prev_kv=False):
        create_watch = etcdrpc.WatchCreateRequest()
        create_watch.key = utils.to_bytes(key)
        if range_end is not None:
            create_watch.range_end = utils.to_bytes(range_end)
        if start_revision is not None:
            create_watch.start_revision = start_revision
        if progress_notify:
            create_watch.progress_notify = progress_notify
        if filters is not None:
            create_watch.filters = filters
        if prev_kv:
            create_watch.prev_kv = prev_kv
        rq = etcdrpc.WatchRequest(create_request=create_watch)

        async with self._lock:
            print("watch.add_callback 1")
            # Start the callback thread if it is not yet running.
            if not self._callback_thread:
                self._callback_thread = create_task(self._run())

            # Only one create watch request can be pending at a time, so if
            # there one already, then wait for it to complete first.
            while self._new_watch:
                await self._new_watch_cond.wait()
            print("watch.add_callback 2")

            # Submit a create watch request.
            new_watch = _NewWatch(callback)
            await self._request_queue.put(rq)
            self._new_watch = new_watch

            # Wait for the request to be completed, or timeout.
            try:
                await asyncio.wait_for(self._new_watch_cond.wait(),
                                       self.timeout)
            except asyncio.TimeoutError:
                await self._lock.acquire()
            self._new_watch = None

            # If the request not completed yet, then raise a timeout exception.
            if new_watch.id is None and new_watch.err is None:
                raise exceptions.WatchTimedOut()
            print("watch.add_callback 3")

            # Raise an exception if the watch request failed.
            if new_watch.err:
                raise new_watch.err

            # Wake up threads stuck on add_callback call if any.
            self._new_watch_cond.notify_all()
            return new_watch.id

    async def cancel(self, watch_id):
        print("cancelling watch", watch_id)
        async with self._lock:
            callback = self._callbacks.pop(watch_id, None)
            if not callback:
                return

            await self._cancel_no_lock(watch_id)
        print("cancelled watch", watch_id)

    async def _run(self):
        while True:
            print("stub.Watch")
            response_iter = self._watch_stub.Watch(
                _new_request_iter(self._request_queue),
                credentials=self._credentials,
                metadata=self._metadata)
            print("stub.Watch returned")
            try:
                async for rs in response_iter:
                    await self._handle_response(rs)

            except grpc.RpcError as err:
                async with self._lock:
                    if self._new_watch:
                        self._new_watch.err = err
                        self._new_watch_cond.notify_all()

                    callbacks = self._callbacks
                    self._callbacks = {}

                    # Rotate request queue. This way we can terminate one gRPC
                    # stream and initiate another one whilst avoiding a race
                    # between them over requests in the queue.
                    print("rotate request queue")
                    await self._request_queue.put(None)
                    self._request_queue = asyncio.Queue(maxsize=10)

                for callback in callbacks.values():
                    await _safe_callback(callback, err)

    async def _handle_response(self, rs):
        async with self._lock:
            if rs.created:
                # If the new watch request has already expired then cancel the
                # created watch right away.
                if not self._new_watch:
                    await self._cancel_no_lock(rs.watch_id)
                    return

                if rs.compact_revision != 0:
                    self._new_watch.err = exceptions.RevisionCompactedError(
                        rs.compact_revision)
                    return

                self._callbacks[rs.watch_id] = self._new_watch.callback
                self._new_watch.id = rs.watch_id
                self._new_watch_cond.notify_all()

            callback = self._callbacks.get(rs.watch_id)

        # Ignore leftovers from canceled watches.
        if not callback:
            return

        # The watcher can be safely reused, but adding a new event
        # to indicate that the revision is already compacted
        # requires api change which would break all users of this
        # module. So, raising an exception if a watcher is still
        # alive.
        if rs.compact_revision != 0:
            err = exceptions.RevisionCompactedError(rs.compact_revision)
            await _safe_callback(callback, err)
            self.cancel(rs.watch_id)
            return

        for event in rs.events:
            await _safe_callback(callback, events.new_event(event))

    async def _cancel_no_lock(self, watch_id):
        cancel_watch = etcdrpc.WatchCancelRequest()
        cancel_watch.watch_id = watch_id
        rq = etcdrpc.WatchRequest(cancel_request=cancel_watch)
        await self._request_queue.put(rq)


class _NewWatch(object):
    def __init__(self, callback):
        self.callback = callback
        self.id = None
        self.err = None


async def _new_request_iter(_request_queue):
    while True:
        rq = await _request_queue.get()
        if rq is None:
            return
        yield rq


async def _safe_callback(callback, event_or_err):
    try:
        await callback(event_or_err)

    except Exception:
        _log.exception('Watch callback failed')
