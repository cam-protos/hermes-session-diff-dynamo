"""Run with a Hermes Python environment (plus moto). Every write is under a temporary home and moto's in-memory AWS."""

import importlib.util
import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='hermes-session-diff-') as tmp:
        root = Path(tmp)
        home = root / 'home'
        home.mkdir()
        workspace = root / 'workspace'
        workspace.mkdir()
        os.environ['HERMES_HOME'] = str(home)
        os.environ['TERMINAL_ENV'] = 'local'
        os.environ['TERMINAL_CWD'] = str(workspace)
        os.environ['HERMES_DISABLE_FILE_STATE_GUARD'] = '1'
        for key in list(os.environ):
            if key.endswith(('_API_KEY', '_TOKEN')):
                os.environ.pop(key, None)
        for key, value in {'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_SESSION_TOKEN': 'testing',
                           'AWS_DEFAULT_REGION': 'us-east-1', 'SESSION_DIFF_TABLE': 'session-diff-integration',
                           'SESSION_DIFF_BUCKET': 'session-diff-integration', 'SESSION_DIFF_REGION': 'us-east-1'}.items():
            os.environ[key] = value
        import boto3
        from moto import mock_aws
        moto = mock_aws()
        moto.start()
        sys.path.insert(0, str(PLUGIN))
        from session_diff.store import table_definition
        boto3.client('dynamodb', region_name='us-east-1').create_table(**table_definition('session-diff-integration'))
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='session-diff-integration')
        installed = home / 'plugins' / 'hermes-session-diff'
        if args.archive:
            with zipfile.ZipFile(args.archive) as archive:
                archive.extractall(home / 'plugins')
        else:
            shutil.copytree(PLUGIN, installed, ignore=shutil.ignore_patterns('node_modules', '.git', '__pycache__', '.impeccable', 'dist'))
        (home / 'config.yaml').write_text('plugins:\n  enabled: [hermes-session-diff]\nterminal:\n  env: local\n', encoding='utf-8')

        from hermes_cli.plugin_validate import validate_plugin_dir
        report = validate_plugin_dir(installed)
        assert report.ok, report.to_dict()
        print('Native plugin validation passed')

        from hermes_cli.web_server_dashboard import _discover_dashboard_plugins
        discovered = _discover_dashboard_plugins()
        assert any(p['name'] == 'hermes-session-diff' and p['has_api'] for p in discovered), discovered

        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        manager.discover_and_load()
        assert manager.has_middleware('tool_execution')
        manager.invoke_hook('pre_llm_call', session_id='integration-session')
        from tools.terminal_tool import register_task_env_overrides
        register_task_env_overrides('integration-session', {'cwd': str(workspace)})
        from model_tools import handle_function_call
        from tools import file_tools
        result = handle_function_call('write_file', {'path': 'example.txt', 'content': 'first\n'},
                                      task_id='integration-session', session_id='integration-session', tool_call_id='write-1')
        assert (workspace / 'example.txt').read_text() == 'first\n', result
        result = handle_function_call('patch', {'path': 'example.txt', 'old_string': 'first', 'new_string': 'second'},
                                      task_id='integration-session', session_id='integration-session', tool_call_id='patch-1')
        assert (workspace / 'example.txt').read_text() == 'second\n', result

        # The dashboard loader imports plugin_api.py by filename, independently of register().
        spec = importlib.util.spec_from_file_location('session_diff_integration_api', installed / 'dashboard' / 'plugin_api.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(module.router, prefix='/api/plugins/hermes-session-diff')
        with TestClient(app) as client:
            base = '/api/plugins/hermes-session-diff'
            view = client.get(base + '/changes', params={'session': 'integration-session'}).json()
            assert len(view['files']) == 1, view
            file = view['files'][0]
            assert file['edits'] == 2, file
            diff = client.get(base + '/diff', params={'session': 'integration-session', 'first': file['first_id'], 'last': file['last_id']})
            assert diff.status_code == 200 and '+second' in diff.json()['patch'], diff.text
            assert client.get(base + '/diff', params={'session': 'another', 'first': file['first_id'], 'last': file['last_id']}).status_code == 404
            assert client.get(base + '/diff', params={'session': 'integration-session', 'first': -1, 'last': 1}).status_code == 422
            print('Real file tools -> middleware -> persistent journal -> HTTP diff passed')


if __name__ == '__main__':
    main()
