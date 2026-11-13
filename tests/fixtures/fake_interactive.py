#!/usr/bin/env python3
"""Offline duplex CLI for both live-input protocols; no real provider calls."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

codex = 'app-server' in sys.argv
sid = 'fake-thread'
if '--resume' in sys.argv:
    sid = sys.argv[sys.argv.index('--resume') + 1]
inputs, prompt, deadline, ended = [], '', None, False
late_inputs = []
buffer = b''

def emit(data):
    print(json.dumps(data), flush=True)

def claude(kind, **data):
    emit({'type': kind, 'session_id': sid, **data})

def event(method, **data):
    emit({'method': method, 'params': {'threadId': sid, 'turnId': 'turn-1', **data}})

def result():
    global ended
    if prompt == 'durable-boundary':
        if codex:
            event('item/started', item={'type': 'agentMessage', 'id': 'old-answer', 'phase': 'final_answer'})
            event('item/completed', item={'type': 'agentMessage', 'id': 'old-answer', 'phase': 'final_answer', 'text': 'OLD COMPLETE ANSWER'})
            event('item/started', item={'type': 'agentMessage', 'id': 'unfinished', 'phase': 'commentary'})
            event('item/agentMessage/delta', itemId='unfinished', delta='UNFINISHED PARTIAL')
        else:
            claude('result', result='OLD COMPLETE ANSWER', is_error=False)
            for item in inputs:
                claude('user', uuid=item['uuid'], isReplay=True, message=item['message'])
            claude('stream_event', event={'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'UNFINISHED PARTIAL'}})
        ended = True
        return
    recovering_boundary = 'Original request:\ndurable-boundary' in prompt
    if prompt == 'boundary' and inputs and not codex:
        claude('result', result='OLD RESULT', is_error=False)
    if not codex:
        for item in inputs:
            if prompt not in {'error', 'noack'}:
                claude('user', uuid=item['uuid'], isReplay=True, message=item['message'])
        claude('result', result='FOLLOWUP COMPLETE ANSWER' if recovering_boundary else
               'ANSWER ' + prompt + ' ' + ' '.join(i['message']['content'] for i in inputs), is_error=prompt == 'error')
    else:
        text = 'FOLLOWUP COMPLETE ANSWER' if recovering_boundary else 'ANSWER ' + prompt + ' ' + ' '.join(i['text'] for i in inputs)
        event('item/started', item={'type': 'agentMessage', 'id': 'comment', 'phase': 'commentary'})
        event('item/agentMessage/delta', itemId='comment', delta='COMMENTARY')
        event('item/completed', item={'type': 'agentMessage', 'id': 'comment', 'phase': 'commentary', 'text': 'COMMENTARY'})
        emit({'method': 'item/agentMessage/delta', 'params': {'threadId': 'child', 'turnId': 'turn-1', 'itemId': 'child', 'delta': 'CHILD'}})
        if prompt == 'multi-final':
            event('item/started', item={'type': 'agentMessage', 'id': 'first-answer', 'phase': 'final_answer'})
            for _ in range(2):
                event('item/completed', item={'type': 'agentMessage', 'id': 'first-answer', 'phase': 'final_answer', 'text': 'FIRST COMPLETE ANSWER'})
        event('item/started', item={'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer'})
        event('item/agentMessage/delta', itemId='answer', delta=text[:7])
        event('item/agentMessage/delta', itemId='answer', delta=text[7:])
        event('item/completed', item={'type': 'agentMessage', 'id': 'answer', 'phase': 'final_answer', 'text': text})
        event('turn/completed', turn={'id': 'turn-1', 'status': 'completed'})
        for request_id, input_id in late_inputs:
            if prompt == 'late-rpc':
                emit({'id': request_id, 'result': {'turnId': 'turn-1'}})
            else:
                event('item/completed', item={'type': 'userMessage', 'id': 'late', 'clientId': input_id})
    ended = True

while True:
    if deadline is not None and not ended and (time.monotonic() >= deadline or Path('release').exists()):
        result()
    readable, _, _ = select.select([sys.stdin], [], [], .01)
    if not readable:
        continue
    chunk = os.read(sys.stdin.fileno(), 65536)
    if not chunk:
        break
    buffer += chunk
    while b'\n' in buffer:
        line, buffer = buffer.split(b'\n', 1)
        raw = json.loads(line)
        with open('wire-input.jsonl', 'a') as f:
            f.write(json.dumps(raw) + '\n')
        method = raw.get('method')
        if codex:
            request_id = raw.get('id')
            params = raw.get('params', {})
            if method == 'initialize':
                emit({'id': request_id, 'result': {}})
            elif method in {'thread/start', 'thread/resume'}:
                sid = params.get('threadId', sid)
                emit({'id': request_id, 'result': {'thread': {'id': sid}, 'model': 'fake-model'}})
            elif method == 'turn/start':
                prompt = params['input'][0]['text']
                if prompt.endswith('REJECT START'):
                    emit({'id': request_id, 'error': {'code': -32602, 'message': 'invalid turn configuration'}})
                    continue
                if prompt.endswith('DELAY START') and not Path('allow-start').exists():
                    Path('turn-start-written').touch()
                    continue
                emit({'id': request_id, 'result': {'turn': {'id': 'turn-1'}}})
                event('turn/started', turn={'id': 'turn-1'})
                event('item/started', item={'id': 'tool', 'type': 'commandExecution', 'command': 'wait'})
                if prompt == 'server-request':
                    emit({'id': 'server-1', 'method': 'item/tool/requestUserInput', 'params': {}})
            elif method == 'turn/steer':
                if prompt == 'reject':
                    emit({'id': request_id, 'error': {'code': -32600, 'message': 'no active turn to steer'}})
                elif prompt in {'wrong-ack', 'malformed-ack'}:
                    emit({'id': request_id, 'result': {'turnId': 'another-turn'} if prompt == 'wrong-ack' else {}})
                elif prompt in {'late-rpc', 'late-echo'}:
                    late_inputs.append((request_id, params['clientUserMessageId']))
                elif prompt not in {'noack', 'drop-interrupt'}:
                    inputs.append({'text': params['input'][0]['text']})
                    emit({'id': request_id, 'result': {'turnId': 'turn-1'}})
                    if prompt != 'rpc-only':
                        event('item/completed', item={'type': 'userMessage', 'id': 'u', 'clientId': params['clientUserMessageId']})
            elif method == 'turn/interrupt':
                if prompt == 'drop-interrupt':
                    event('turn/completed', turn={'id': 'turn-1', 'status': 'interrupted'})
                    sys.exit(0)
                emit({'id': request_id, 'result': {}})
                event('turn/completed', turn={'id': 'turn-1', 'status': 'interrupted'})
                ended = True
        elif not prompt:
            prompt = raw['message']['content']
            claude('system', subtype='init', model='fake-model')
            claude('user', uuid=raw['uuid'], isReplay=True, message=raw['message'])
            claude('assistant', message={'content': [{'type': 'tool_use', 'name': 'wait', 'id': 'tool'}]})
        else:
            inputs.append(raw)
        if prompt and deadline is None:
            deadline = time.monotonic() + (60 if prompt in {'held', 'noack', 'child', 'drop-interrupt'} else .35)
            if prompt == 'child':
                child = subprocess.Popen(['sleep', '60'], start_new_session=True)
                Path('child.pid').write_text(str(child.pid))
