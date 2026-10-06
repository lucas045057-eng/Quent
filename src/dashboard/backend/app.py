from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi import HTTPException
import re
from uuid import UUID

from .config import DashboardConfig
from .models import envelope
from .security import trusted_request
from .service import DashboardService
from .backtests import BacktestReader
from .decisions import DecisionReader
from .events import EventReader
from .files import FileCatalog


def create_app(config=None):
    config = config or DashboardConfig.from_env()
    app = FastAPI(title='Quant Local Operations', docs_url=None, redoc_url=None, openapi_url=None)
    service = DashboardService(config)
    app.state.service = service
    catalog = FileCatalog(config.root)
    decisions = DecisionReader(service.database)
    backtests = BacktestReader(catalog)
    events = EventReader(catalog, service.database)

    @app.middleware('http')
    async def local_guard(request: Request, call_next):
        if not trusted_request(request, config.port):
            response = JSONResponse(envelope({'error':'LOCAL_ORIGIN_REQUIRED'}, 'ERROR'), status_code=403)
        elif request.method not in {'GET', 'HEAD'}:
            response = JSONResponse(envelope({'error':'READ_ONLY_API'}, 'ERROR'), status_code=405)
        else:
            try:
                response = await call_next(request)
            except Exception:
                response = JSONResponse(envelope({'error':'SOURCE_UNAVAILABLE'}, 'ERROR'), status_code=503)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(envelope({'error':'INVALID_REQUEST'}, 'ERROR'), status_code=422)

    @app.get('/api/health')
    def health():
        return service.health()

    @app.get('/api/strategy-v2')
    def strategy_v2():
        return service.strategy_v2()

    @app.get('/api/risk-policy')
    def risk_policy():
        return service.risk_policy()

    @app.get('/api/overview')
    def overview():
        return service.overview()

    @app.get('/api/paper')
    def paper():
        return service.paper()

    @app.get('/api/realtime-paper')
    def realtime_paper():
        return service.realtime_paper()

    @app.get('/api/positions')
    def positions(limit: int = Query(100, ge=1, le=200), session: str | None = Query(None, pattern=r'^[0-9a-f]{24}$')):
        return service.positions(limit, session)

    @app.get('/api/orders')
    def orders(limit: int = Query(100, ge=1, le=200), session: str | None = Query(None, pattern=r'^[0-9a-f]{24}$')):
        return service.orders(limit, session)

    @app.get('/api/trades')
    def trades(limit: int = Query(100, ge=1, le=200), session: str | None = Query(None, pattern=r'^[0-9a-f]{24}$')):
        return service.trades(limit, session)

    @app.get('/api/decisions')
    def decision_list(limit: int = Query(100,ge=1,le=200), symbol: str | None = Query(None,max_length=32), eligible: bool | None = None):
        return decisions.list(limit,symbol,eligible)

    @app.get('/api/decisions/{decision_id}')
    def decision_detail(decision_id: UUID):
        return decisions.detail(str(decision_id))

    @app.get('/api/backtests')
    def backtest_list(limit: int = Query(100,ge=1,le=200)):
        return backtests.list(limit)

    @app.get('/api/backtests/{run_id}')
    def backtest_detail(run_id: str):
        return backtests.detail(run_id)

    @app.get('/api/logs')
    @app.get('/api/events')
    def logs(level: str | None = Query(None,pattern=r'^(DEBUG|INFO|WARNING|ERROR|CRITICAL|UNKNOWN)$'),
             module: str | None = Query(None,max_length=64), q: str | None = Query(None,max_length=128),
             limit: int = Query(100,ge=1,le=200)):
        return events.list(level,module,q,limit)

    dist = config.root/'dashboard/frontend/dist'

    @app.get('/assets/{name:path}')
    def asset(name: str):
        target = dist/'assets'/name
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.(js|css|svg|png|woff2?)',name) or target.is_symlink() or not target.is_file() or target.resolve().parent != (dist/'assets').resolve():
            raise HTTPException(404)
        return FileResponse(target,headers={'Cache-Control':'public,max-age=31536000,immutable'})

    @app.get('/favicon.ico')
    def favicon():
        return Response(status_code=204)

    def index():
        target = dist/'index.html'
        if not target.is_file() or target.is_symlink():
            raise HTTPException(404)
        return FileResponse(target,headers={'Cache-Control':'no-cache'})

    for path in ('/','/overview','/paper','/realtime-paper','/decisions','/backtests','/health','/logs','/strategy','/risk-policy'):
        app.add_api_route(path,index,methods=['GET'],include_in_schema=False)

    return app
