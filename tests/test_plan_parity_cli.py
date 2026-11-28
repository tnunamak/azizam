"""The owner parity command selects logical Responses, pin control, then chat."""
import json
import sys

from scripts import plan_parity_check as check


def test_parity_uses_logical_id_then_explicit_pin_control(monkeypatch, capsys):
    calls = []
    def responses(model, effort, prompts):
        calls.append(('responses', model))
        return [{'input': 1, 'cached': 0, 'output': 2, 'reasoning': 0}]
    def chat(model, effort, prompts):
        calls.append(('chat', model))
        return [{'input': 1, 'cached': 0, 'output': 2, 'reasoning': 0}]
    monkeypatch.setattr(check, 'run_responses', responses)
    monkeypatch.setattr(check, 'run_chat', chat)
    monkeypatch.setattr(sys, 'argv', ['plan_parity_check', '--model', 'model-test', '--route-pin', 'route/model-test', '--skip-codex', '--turns', '1'])
    check.main()
    assert calls == [('responses', 'model-test'), ('responses', 'route/model-test'), ('chat', 'model-test')]
    assert 'pinned control' in capsys.readouterr().out


def test_catalog_deploy_check_exercises_each_pin_and_both_surfaces():
    before = {'data': [{'id': 'plan/alpha'}, {'id': 'plan/beta'}, {'id': 'unchanged'}]}
    after = {'data': [{'id': 'alpha'}, {'id': 'beta'}, {'id': 'unchanged'}]}
    calls = []
    def get(path):
        calls.append(('GET', path))
        if path == '/v1/models':
            return after
        return {'id': 'plan/' + ('alpha' if 'alpha' in path else 'beta')}
    def post(path, body):
        calls.append(('POST', path, body['model']))
        return {'id': 'answer', '_parity_facts': {'route': body['model']}}
    result = check.check_catalog_publication(before, pin_prefix='plan/', get=get, post=post)
    assert result['ids_in'] == ['alpha', 'beta']
    assert result['ids_out'] == ['plan/alpha', 'plan/beta']
    assert [call[1:] for call in calls if call[0] == 'POST'] == [
        ('/v1/responses', 'plan/alpha'), ('/v1/chat/completions', 'plan/alpha'),
        ('/v1/responses', 'plan/beta'), ('/v1/chat/completions', 'plan/beta')]
    assert sum(call[0] == 'GET' for call in calls) == 3


def test_catalog_deploy_check_refuses_a_pin_left_in_the_ordinary_listing():
    import pytest
    before = {'data': [{'id': 'plan/alpha'}]}
    with pytest.raises(ValueError, match='pin.*listing'):
        check.check_catalog_publication(before, pin_prefix='plan/',
            get=lambda path: {'data': [{'id': 'plan/alpha'}, {'id': 'alpha'}]},
            post=lambda *a: (_ for _ in ()).throw(AssertionError('must stop before any provider call')))


def test_tool_loop_sends_one_turn_of_tool_results_and_reports_the_turn_state(monkeypatch, capsys):
    sent = []

    def post(path, body):
        sent.append(json.loads(json.dumps(body)))  # the script keeps growing one history list
        last = body['input'][-1]
        if last.get('type') == 'function_call_output' and last['output'] == 'done':
            output = [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'finished'}]}]
        else:
            n = 1 if last.get('type') != 'function_call_output' else int(last['output'])
            output = [{'type': 'function_call', 'call_id': f'c{n}', 'name': 'next_step', 'arguments': f'{{"n": {n}}}'}]
        state = 'started' if len(sent) == 1 else 'replayed'
        return {'output': output, 'usage': {'input_tokens': 2000, 'input_tokens_details': {'cached_tokens': 1900 if len(sent) > 1 else 0}},
                '_parity_facts': {'turn_state': state}}

    monkeypatch.setattr(check, 'post', post)
    monkeypatch.setattr(sys, 'argv', ['plan_parity_check', '--model', 'model-test', '--tool-loop', '3'])
    check.main()
    out = capsys.readouterr().out
    assert len(sent) == 4
    assert all(body['instructions'] == sent[0]['instructions'] for body in sent)
    assert [body['input'][-1].get('output') for body in sent[1:]] == ['2', '3', 'done']
    assert 'follow-up requests with cached input: 3 of 3' in out and 'replayed' in out
