import base64
import threading
import time

import pytest

import aetcd.exceptions
import aetcd.rpc
import aetcd.utils


@pytest.fixture
def etcdctl_put(etcdctl):
    def _etcdctl_put(key, value):
        etcdctl('put', key, value)
        result = etcdctl('get', key)
        assert base64.b64decode(
            result['kvs'][0]['value']) == aetcd.utils.to_bytes(value)
    return _etcdctl_put


@pytest.mark.asyncio
async def test_watch_key(etcdctl, etcd, etcdctl_put):
    def update_key():
        # Sleep to make watch can get the event
        time.sleep(3)
        etcdctl_put('key', '0')
        time.sleep(1)
        etcdctl_put('key', '1')
        time.sleep(1)
        etcdctl_put('key', '2')
        time.sleep(1)
        etcdctl_put('key', '3')
        time.sleep(1)

    t = threading.Thread(name='update_key', target=update_key)
    t.start()

    change_count = 0
    w = await etcd.watch(b'key')
    async for event in w:
        assert event.kv.key == b'key'
        assert event.kv.value == aetcd.utils.to_bytes(str(change_count))

        # If cancel worked, we should not receive event 3
        assert event.kv.value != b'3'

        change_count += 1
        if change_count > 2:
            # If cancel not work, we will block in this for-loop forever
            await w.cancel()

    t.join()


@pytest.mark.asyncio
async def test_watch_key_with_revision_compacted(etcdctl, etcd, etcdctl_put):
    # Some data to compact
    etcdctl('put', 'key', '0')
    result = await etcd.get(b'key')
    revision = result.mod_revision

    # Compact etcd and test watcher
    await etcd.compact(revision)

    def update_key():
        etcdctl_put('key', '1')
        etcdctl_put('key', '2')
        etcdctl_put('key', '3')

    t = threading.Thread(name='update_key', target=update_key)
    t.start()

    async def watch_compacted_revision_test():
        w = await etcd.watch(b'key', start_revision=(revision - 1))

        error_raised = False
        compacted_revision = 0
        try:
            async for _ in w:
                _
        except Exception as e:
            error_raised = True
            assert isinstance(
                e,
                aetcd.exceptions.RevisionCompactedError,
            )
            compacted_revision = e.compacted_revision

        assert error_raised is True
        assert compacted_revision == revision

        change_count = 0
        w = await etcd.watch(b'key', start_revision=compacted_revision)

        async for event in w:
            assert event.kv.key == b'key'
            assert event.kv.value == aetcd.utils.to_bytes(str(change_count))

            # If cancel worked, we should not receive event 3
            assert event.kv.value != aetcd.utils.to_bytes('3')

            change_count += 1
            if change_count > 2:
                await w.cancel()

    await watch_compacted_revision_test()

    t.join()


@pytest.mark.asyncio
async def test_watch_key_prefix(etcdctl, etcd, etcdctl_put):
    def update_key_prefix():
        # Sleep to make watch can get the event
        time.sleep(3)
        etcdctl_put('/key0', '0')
        time.sleep(1)
        etcdctl_put('/key1', '1')
        time.sleep(1)
        etcdctl_put('/key2', '2')
        time.sleep(1)
        etcdctl_put('/key3', '3')
        time.sleep(1)

    t = threading.Thread(name='update_key_prefix', target=update_key_prefix)
    t.start()

    change_count = 0
    w = await etcd.watch_prefix(b'/key')
    async for event in w:
        assert event.kv.key == aetcd.utils.to_bytes(f'/key{change_count}')
        assert event.kv.value == aetcd.utils.to_bytes(str(change_count))

        # If cancel worked, we should not receive event 3
        assert event.kv.value != b'3'

        change_count += 1
        if change_count > 2:
            # If cancel not work, we will block in this for-loop forever
            await w.cancel()

    t.join()


@pytest.mark.asyncio
async def test_watch_key_prefix_once_sequential(etcd):
    with pytest.raises(aetcd.exceptions.WatchTimeoutError):
        await etcd.watch_prefix_once(b'/key', 1)

    with pytest.raises(aetcd.exceptions.WatchTimeoutError):
        await etcd.watch_prefix_once(b'/key', 1)

    with pytest.raises(aetcd.exceptions.WatchTimeoutError):
        await etcd.watch_prefix_once(b'/key', 1)