"""Local stdio Codex app-server, one supervised process per ordinary turn."""
import asyncio
import json

from .base import Event, SteerResult
from .codex import CodexExecutor
from .interactive import InteractiveExecutor


class CodexAppExecutor(InteractiveExecutor, CodexExecutor):
    def command(self, session_id, model=None):
        return [self.binary, 'app-server', '--listen', 'stdio://', *self.extra_args]

    async def rpc(self, run, method, params):
        run.next_id += 1
        request_id = run.next_id
        future = asyncio.get_running_loop().create_future()
        run.requests[request_id] = future
        run.request_methods[request_id] = method
        await run.write({'id': request_id, 'method': method, 'params': params})
        try:
            return await future
        finally:
            run.requests.pop(request_id, None)
            run.request_methods.pop(request_id, None)

    async def initialize(self, run, prompt, model):
        await self.rpc(run, 'initialize', {'clientInfo': {'name': 'eivoh', 'version': '0.1.0'},
                                          'capabilities': {'experimentalApi': True}})
        await run.write({'method': 'initialized'})
        params = {'cwd': str(self.workdir), 'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}
        if model or self.model:
            params['model'] = model or self.model
        if run.session_id:
            params['threadId'] = run.session_id
        response = await self.rpc(run, 'thread/resume' if run.session_id else 'thread/start', params)
        run.session_id = response['thread']['id']
        response = await self.rpc(run, 'turn/start', {'threadId': run.session_id,
            'input': [{'type': 'text', 'text': prompt}], **({'model': model or self.model} if model or self.model else {})})
        run.turn_id = response['turn']['id']
        run.ready.set()

    async def send_steer(self, run, input_id, text):
        run.next_id += 1
        request_id = run.next_id
        run.items[('request', request_id)] = input_id
        await run.write({'id': request_id, 'method': 'turn/steer', 'params': {
            'threadId': run.session_id, 'expectedTurnId': run.turn_id,
            'clientUserMessageId': input_id, 'input': [{'type': 'text', 'text': text}]}})

    async def receive(self, run, raw):
        events = []
        def add(kind, **data):
            events.append(Event(kind, data, raw, run.session_id))
        def started(turn_id):
            if run.terminal:
                return
            run.turn_id = turn_id
            if not run.started:
                run.started = True
                add('input_accepted', initial=True)
        if 'id' in raw and 'method' not in raw:
            request_id = raw['id']
            input_id = run.items.pop(('request', request_id), None)
            if input_id is not None:
                if raw.get('error'):
                    future = run.inputs[input_id]
                    if not future.done():
                        future.set_result(SteerResult('rejected', str(raw['error'].get('message', 'Rejected'))))
                elif run.terminal:
                    pass  # Receipt after the turn ended cannot join its result.
                elif isinstance(raw.get('result'), dict) and raw['result'].get('turnId') == run.turn_id:
                    add('input_accepted', input_id=input_id)
                else:
                    run.accepting = False
                    future = run.inputs[input_id]
                    if not future.done():
                        future.set_result(SteerResult('unknown', 'Invalid steering acknowledgement'))
            elif future := run.requests.get(request_id):
                if not future.done():
                    if raw.get('error'):
                        future.set_exception(RuntimeError(str(raw['error'])))
                    else:
                        result = raw.get('result') or {}
                        if 'thread' in result:
                            run.session_id = result['thread']['id']
                            run.configured_model = result.get('model')
                            # Preserve identity even if the process dies before
                            # turn/start's response. Identity is not acceptance.
                            add('session_started', session_id=run.session_id, input_accepted=False,
                                configured_model=run.configured_model, configured_model_source='thread.response')
                        if run.request_methods.get(request_id) == 'turn/start' and (result.get('turn') or {}).get('id'):
                            started(result['turn']['id'])
                        future.set_result(result)
            return events
        if 'id' in raw and 'method' in raw:
            # No interactive approvals in this service. Explicitly reject every
            # unsupported server request instead of leaving its future hanging.
            await run.write({'id': raw['id'], 'error': {'code': -32601,
                             'message': 'Eivoh does not support interactive server requests'}})
            add('approval_requested', actionable=False, method=raw['method'])
            return events
        method, params = raw.get('method'), raw.get('params') or {}
        if run.terminal:
            return events  # Retain trailing raw lines, not trailing answer/ack events.
        if params.get('threadId') and params['threadId'] != run.session_id:
            return events  # Child threads must not become the parent's answer.
        if method == 'turn/started':
            started(params['turn']['id'])
        if params.get('turnId') and run.turn_id and params['turnId'] != run.turn_id:
            return events
        if method in {'item/started', 'item/completed'}:
            item = params.get('item') or {}
            kind, item_id = item.get('type'), item.get('id')
            if kind == 'userMessage' and item.get('clientId') in run.inputs:
                add('input_accepted', input_id=item['clientId'])
            elif kind == 'agentMessage':
                state = run.items.setdefault(item_id, {'text': '', 'phase': item.get('phase')})
                state['phase'] = item.get('phase') or state.get('phase')
                if method == 'item/completed':
                    state['text'] = item.get('text', state['text'])
                    run.last_message = state['text']
                    if state['phase'] == 'final_answer':
                        part_id = 'codex:' + json.dumps([run.session_id, run.turn_id, item_id])
                        run.answer_parts[part_id] = state['text']
                        add('answer_part', id=part_id, text=state['text'])
                    add('message', text=state['text'], message_id=item_id, replace=True, phase=state['phase'])
            elif kind in {'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch', 'dynamicToolCall'}:
                add('tool_started' if method == 'item/started' else 'tool_finished',
                    name=item.get('command') or kind, id=item_id)
        elif method == 'item/agentMessage/delta':
            item_id = params['itemId']
            state = run.items.setdefault(item_id, {'text': '', 'phase': None})
            first = not state['text']
            state['text'] += params.get('delta', '')
            add('text_delta', text=params.get('delta', ''), message_id=item_id, replace=first, phase=state['phase'])
        elif method == 'thread/tokenUsage/updated':
            add('usage', usage=params.get('tokenUsage', {}))
        elif method == 'turn/completed':
            turn = params.get('turn') or {}
            if run.turn_id and turn.get('id') != run.turn_id:
                return events
            run.accepting = False
            run.terminal = True
            status = turn.get('status')
            if status != 'completed':
                add('error', message=(turn.get('error') or {}).get('message') or f'Codex turn {status}')
            add('turn_completed', status='completed' if status == 'completed' else 'failed',
                text='\n\n'.join(run.answer_parts.values()) if run.answer_parts else run.last_message,
                answer_part_ids=list(run.answer_parts))
        return events

    async def cancel(self, run_id):
        run = self.runs.get(run_id)
        if run and run.turn_id and not run.finished and run.proc.returncode is None:
            run.accepting = False
            try:
                async with asyncio.timeout(0.5):
                    await self.rpc(run, 'turn/interrupt', {'threadId': run.session_id, 'turnId': run.turn_id})
            except (TimeoutError, RuntimeError, BrokenPipeError, ConnectionResetError):
                pass
        await super().cancel(run_id)
