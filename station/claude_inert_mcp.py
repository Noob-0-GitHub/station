"""Inert MCP server: lists Station's tools to the Claude Code CLI, executes nothing.

Copied from Hermes's plugin claude-subscription-directsdk (MIT, see station/licenses/hermes-official-plugins.txt).
Run with `python -I`; no Station imports.
"""
import json
from pathlib import Path
import sys


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    for line in sys.stdin:
        row = json.loads(line)
        method = row.get('method')
        result = {}
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'station-inert-inventory', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': manifest}
        elif method == 'tools/call':
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Denied: native tools are inert; only Station executes tools.'}]}
        if 'id' in row:
            print(json.dumps({'jsonrpc': '2.0', 'id': row['id'], 'result': result}), flush=True)


if __name__ == '__main__':
    main()
