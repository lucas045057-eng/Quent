import argparse
from dataclasses import replace
import json
from pathlib import Path
from uuid import UUID

from .config import DashboardConfig
from .lifecycle import start, stop, status, verify_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local read-only Quant dashboard')
    parser.add_argument('command',choices=('serve','start','stop','status'))
    parser.add_argument('--root',type=Path)
    parser.add_argument('--port',type=int)
    parser.add_argument('--host',choices=('127.0.0.1',),default='127.0.0.1')
    parser.add_argument('--instance',type=UUID)
    parser.add_argument('--hold',action='store_true',help='Keep WSL host alive while our dashboard child runs')
    args = parser.parse_args(argv)
    try:
        config = DashboardConfig.from_env()
        config = replace(config,root=args.root or config.root,port=args.port or config.port)
        if args.command == 'serve':
            import uvicorn
            from .app import create_app
            verify_bundle(config.root)
            uvicorn.run(create_app(config),host='127.0.0.1',port=config.port,
                access_log=False,log_level='critical',server_header=False)
            return 0
        result = {'start':lambda:start(config,hold=args.hold),'stop':lambda:stop(config),'status':lambda:status(config)}[args.command]()
        print(json.dumps(result),flush=True)
        return 0
    except Exception as error:
        codes = {'INVALID_LOCAL_CONFIGURATION','PORT_IN_USE','DASHBOARD_START_FAILED',
            'BUNDLE_SOURCE_MISMATCH','BUNDLE_ASSET_MISMATCH','BUNDLE_MISSING_OR_INVALID',
            'UNSAFE_RUNTIME_DIRECTORY','DASHBOARD_OPERATION_IN_PROGRESS'}
        code = str(error) if isinstance(error,ValueError) and str(error) in codes else 'DASHBOARD_OPERATION_FAILED'
        print(json.dumps(dict(state='ERROR',code=code)),flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
