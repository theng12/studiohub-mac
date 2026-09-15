import httpx
import pytest

from backend import broker


@pytest.fixture
def protection(reset):
    broker._model_protection.clear()
    broker.MODEL_PROTECTION_FILE.unlink(missing_ok=True)
    yield
    broker._model_protection.clear()
    broker.MODEL_PROTECTION_FILE.unlink(missing_ok=True)


def test_missing_weights_block_only_this_studio_model_and_survive_reload(protection):
    studio = {'id': 'image@node-a', 'machine': 'node-a'}
    broker._mark_model_failure(studio, 'model-a', 'abc', 'MODEL_FILES_UNAVAILABLE', 'Cannot read model files')
    assert broker._model_block_note(studio, 'model-a', {})
    assert not broker._model_block_note(studio, 'model-b', {})
    assert not broker._model_block_note({'id': 'image@node-b'}, 'model-a', {})
    broker._model_protection.clear()
    broker._load_model_protection()
    assert broker.model_protection_snapshot()[0]['blocked'] is True
    broker._mark_machine_success(studio)
    assert broker._model_block_note(studio, 'model-a', {})


def test_repeated_engine_failures_block_but_one_failure_does_not(protection):
    studio = {'id': 'voice@node-a'}
    for _ in range(2):
        broker._mark_model_failure(studio, 'model-a', None, 'WORKER_TERMINAL_ERROR', 'RuntimeError: engine failed')
    assert not broker._model_block_note(studio, 'model-a', {})
    broker._mark_model_failure(studio, 'model-a', None, 'WORKER_TERMINAL_ERROR', 'RuntimeError: engine failed')
    assert broker._model_block_note(studio, 'model-a', {})


def test_failure_classifier_distinguishes_model_files_from_capacity_and_bad_input(protection):
    assert broker._model_file_failure('RuntimeError: [load_safetensors] Failed to open file /private/model', None)
    assert broker._model_file_failure('RuntimeError: [load_safetensors] Failed to open /private/model/text_encoder/0.safetensors', None)
    assert not broker._model_file_failure('MemoryGuardError: not enough memory', None)
    assert not broker._model_file_failure('ValueError: invalid prompt', None)
    assert broker._worker_terminal_error('missing', 'MODEL_FILES_UNAVAILABLE').model_unavailable


@pytest.mark.asyncio
async def test_recheck_requires_explicit_fresh_readiness_not_legacy_cached(protection, monkeypatch):
    studio = next(s for s in broker._monitor().registry if s['id'] == 'image')
    broker._mark_model_failure(studio, 'model-a', None, 'MODEL_FILES_UNAVAILABLE', 'missing')
    payload = {'models': [{'repo': 'model-a', 'cache': {'state': 'cached'}}]}
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            return httpx.Response(200, json=payload, request=httpx.Request('GET', url))
    monkeypatch.setattr(broker.httpx, 'AsyncClient', Client)
    with pytest.raises(ValueError, match='readiness'):
        await broker.recheck_model_protection('image', 'model-a')
    assert broker._model_block_note(studio, 'model-a', {})
    payload['models'][0]['readiness'] = {'ready': True, 'revision': 'a' * 40}
    assert (await broker.recheck_model_protection('image', 'model-a'))['ok']
    assert not broker._model_block_note(studio, 'model-a', {})


@pytest.mark.parametrize("status", [409, 503])
def test_model_rejection_is_not_a_transport_outage(protection, status):
    response = httpx.Response(status, json={"detail": {
        "error_code": "MODEL_FILES_UNAVAILABLE", "reason": "Required shard missing"}})
    error = broker._worker_http_error(response)
    assert error.retryable
    assert error.model_unavailable
    assert not broker._is_transport_failure(error, str(error))


@pytest.mark.asyncio
async def test_recheck_cannot_clear_a_concurrent_failure(protection, monkeypatch):
    studio = next(s for s in broker._monitor().registry if s['id'] == 'image')
    broker._mark_model_failure(studio, 'model-a', None, 'MODEL_FILES_UNAVAILABLE', 'missing')
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            broker._mark_model_failure(studio, 'model-a', None, 'MODEL_FILES_UNAVAILABLE', 'still missing')
            return httpx.Response(200, json={'models': [{'repo': 'model-a', 'readiness': {'ready': True}}]},
                                  request=httpx.Request('GET', url))
    monkeypatch.setattr(broker.httpx, 'AsyncClient', Client)
    with pytest.raises(ValueError, match='new failure'):
        await broker.recheck_model_protection('image', 'model-a')
    assert broker._model_block_note(studio, 'model-a', {})


@pytest.mark.asyncio
async def test_typed_rejection_quarantines_model_without_quarantining_machine(protection):
    submitted = broker.submit_batch({'modality': 'image', 'model': 'model-a', 'items': [{'prompt': 'hello'}]})
    batch = broker.batches[submitted['batch_id']]
    item = batch['items'][0]
    studio = next(s for s in broker._monitor().registry if s['id'] == 'image')
    item.update(state='running', tries=1, studio='image')
    class Client:
        async def post(self, url, **kwargs):
            return httpx.Response(503, json={'detail': {'code': 'MODEL_FILES_UNAVAILABLE', 'reason': 'missing shard'}})
    await broker._run_item(Client(), batch, item, studio)
    assert item['state'] == 'queued'
    assert not item.get('infra_failures')
    assert broker.machine_protection_snapshot() == {}
    assert broker._model_block_note(studio, 'model-a', {})


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_by', ['failure', 'readiness'])
async def test_scheduler_does_not_submit_to_unready_model(protection, monkeypatch, blocked_by):
    import asyncio
    monitor = broker._monitor()
    studio = next(s for s in monitor.registry if s['id'] == 'image')
    monitor.status['image'] = {'status': 'up'}
    entry = {'repo': 'model-a', 'cache': {'state': 'cached'}}
    if blocked_by == 'failure':
        broker._mark_model_failure(studio, 'model-a', None, 'MODEL_FILES_UNAVAILABLE', 'missing shard')
    else:
        entry['readiness'] = {'ready': False, 'reason': 'missing shard'}
    async def catalog(*args): return entry
    submitted_calls = []
    async def run_item(*args): submitted_calls.append(args)
    monkeypatch.setattr(broker, '_catalog_entry', catalog)
    monkeypatch.setattr(broker, '_run_item', run_item)
    submitted = broker.submit_batch({'modality': 'image', 'model': 'model-a', 'items': [{'prompt': 'hello'}]})
    task = asyncio.create_task(broker._dispatch_loop())
    try:
        await asyncio.sleep(0.05)
        item = broker.batches[submitted['batch_id']]['items'][0]
        assert not submitted_calls
        assert item['state'] == 'queued' and item['tries'] == 0
        assert 'missing shard' in broker.batches[submitted['batch_id']]['governor_note']
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_code,can_reopen', [('ENGINE_ERROR', True), ('MODEL_FILES_UNAVAILABLE', False)])
async def test_voice_runtime_readiness_cannot_clear_a_file_failure(protection, monkeypatch, failure_code, can_reopen):
    studio = next(s for s in broker._monitor().registry if s['id'] == 'voice')
    for _ in range(3):
        broker._mark_model_failure(studio, 'model-a', None, failure_code, 'failed')
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, **kwargs):
            return httpx.Response(200, json={'models': [{'repo': 'model-a', 'runtime_ready': True,
                'available': True, 'cache': {'state': 'cached'}}]}, request=httpx.Request('GET', url))
    monkeypatch.setattr(broker.httpx, 'AsyncClient', Client)
    if can_reopen:
        assert (await broker.recheck_model_protection('voice', 'model-a'))['ok']
    else:
        with pytest.raises(ValueError, match='readiness'):
            await broker.recheck_model_protection('voice', 'model-a')
    assert bool(broker._model_block_note(studio, 'model-a', {})) != can_reopen


@pytest.mark.asyncio
async def test_lost_poll_adopts_exact_model_failure_instead_of_transport_failure(protection, monkeypatch):
    submitted = broker.submit_batch({'modality': 'image', 'model': 'model-a', 'items': [{'prompt': 'hello'}]})
    batch = broker.batches[submitted['batch_id']]
    item = batch['items'][0]
    studio = next(s for s in broker._monitor().registry if s['id'] == 'image')
    item.update(state='running', tries=1, studio='image')
    class Client:
        polls = 0
        async def post(self, url, **kwargs):
            return httpx.Response(200, json={'job': {'id': 'accepted-job', 'state': 'queued'}})
        async def get(self, url, **kwargs):
            self.polls += 1
            if self.polls == 1:
                raise httpx.RemoteProtocolError('connection dropped')
            return httpx.Response(200, json={'job': {'id': 'accepted-job', 'state': 'error',
                'error_code': 'MODEL_FILES_UNAVAILABLE', 'error': 'missing shard'}})
    async def no_sleep(*args): pass
    monkeypatch.setattr(broker.asyncio, 'sleep', no_sleep)
    await broker._run_item(Client(), batch, item, studio)
    assert item['state'] == 'queued'
    assert item['error_code'] == 'MODEL_FILES_UNAVAILABLE'
    assert not item.get('infra_failures')
    assert broker.machine_protection_snapshot() == {}
    assert broker.model_protection_snapshot()[0]['failures'] == 1


def test_engine_failure_window_does_not_extend_with_each_failure(protection, monkeypatch):
    studio = {'id': 'voice@node-a'}
    for now in (1000.0, 1240.0, 1480.0):
        monkeypatch.setattr(broker.time, 'time', lambda: now)
        broker._mark_model_failure(studio, 'model-a', None, 'ENGINE_ERROR', 'RuntimeError: failed')
    assert not broker._model_block_note(studio, 'model-a', {})
    assert broker._model_protection[(studio['id'], 'model-a')]['failures'] == 1
