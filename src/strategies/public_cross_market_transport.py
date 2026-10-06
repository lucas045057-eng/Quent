"""GET-only Binance/Bybit public market requests through fixed host routes."""
import json
import os
from pathlib import Path
import subprocess

from quant_phase6.ai import ProviderError, AIErrorCode


SCRIPT = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$inputStream=$null
$response=$null
try {
    $packet=[Console]::In.ReadToEnd() | ConvertFrom-Json
    $allowed=@{
        'fapi.binance.com'=@('/futures/data/openInterestHist','/fapi/v1/premiumIndex','/fapi/v1/fundingRate')
        'api.bybit.com'=@('/v5/market/open-interest','/v5/market/mark-price-kline','/v5/market/tickers','/v5/market/instruments-info','/v5/market/funding/history')
    }
    $results=@{}
    $watch=[Diagnostics.Stopwatch]::StartNew()
    foreach ($item in $packet.requests) {
        try {
            $uri=[Uri]$item.url
            if ($uri.Scheme -ne 'https' -or -not $allowed.ContainsKey($uri.Host) -or $allowed[$uri.Host] -notcontains $uri.AbsolutePath) { throw 'ENDPOINT_BLOCKED' }
            $pairs=@()
            foreach ($property in $item.params.PSObject.Properties) {
                $pairs += [Uri]::EscapeDataString($property.Name)+'='+[Uri]::EscapeDataString([string]$property.Value)
            }
            $target=$uri.AbsoluteUri
            if ($pairs.Count -gt 0) { $target += '?'+($pairs -join '&') }
            $remaining=[int]($packet.timeout_seconds*1000)-[int]$watch.ElapsedMilliseconds
            if ($remaining -lt 1) { throw 'DEADLINE' }
            $web=[Net.HttpWebRequest]::Create($target)
            $web.Method='GET'
            $web.Proxy=[Net.WebRequest]::DefaultWebProxy
            $web.AllowAutoRedirect=$false
            $web.Timeout=$remaining
            $web.ReadWriteTimeout=$remaining
            $web.UserAgent='QuantPaperV2/2.0'
            $web.Accept='application/json'
            $response=$web.GetResponse()
            if ([Uri]$response.ResponseUri -and ([Uri]$response.ResponseUri).Host -ne $uri.Host) { throw 'REDIRECT_BLOCKED' }
            $inputStream=$response.GetResponseStream()
            $memory=New-Object IO.MemoryStream
            $buffer=New-Object byte[] 8192
            while (($count=$inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
                if (($memory.Length+$count) -gt 1048576) { throw 'RESPONSE_TOO_LARGE' }
                $memory.Write($buffer,0,$count)
            }
            $parsed=[Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
            $results[$item.key]=$parsed
            $inputStream.Close(); $inputStream=$null
            $response.Close(); $response=$null
        } catch {
            $results[$item.key]=@{__transport_error='PUBLIC_GET_FAILED'}
            if ($inputStream) { $inputStream.Close(); $inputStream=$null }
            if ($response) { $response.Close(); $response=$null }
        }
    }
    @{http_status=200;body=$results} | ConvertTo-Json -Depth 50 -Compress
} catch {
    @{http_status=0;error='WINDOWS_PUBLIC_TRANSPORT_FAILED'} | ConvertTo-Json -Compress
} finally {
    if ($inputStream) { $inputStream.Close() }
    if ($response) { $response.Close() }
}
"""


def powershell_executable():
    if os.name == 'nt':
        path = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    else:
        path = Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    if not path.is_file():
        raise RuntimeError('PUBLIC_WINDOWS_TRANSPORT_UNAVAILABLE')
    return str(path)


def windows_public_get_many(requests, *, timeout_seconds):
    if not requests or len(requests) > 8 or not 0 < timeout_seconds <= 20:
        raise ValueError('PUBLIC_REQUEST_SCOPE_INVALID')
    packet = json.dumps({'requests': requests, 'timeout_seconds': timeout_seconds}, ensure_ascii=True, allow_nan=False)
    kwargs = {'input': packet, 'text': True, 'encoding': 'utf-8', 'stdout': subprocess.PIPE,
        'stderr': subprocess.DEVNULL, 'timeout': timeout_seconds + 2}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run([powershell_executable(), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', SCRIPT], **kwargs)
        if result.returncode != 0:
            raise RuntimeError('PUBLIC_WINDOWS_TRANSPORT_FAILED')
        envelope = json.loads(result.stdout.lstrip('\ufeff').strip())
        if envelope.get('http_status') != 200 or not isinstance(envelope.get('body'), dict):
            raise RuntimeError('PUBLIC_WINDOWS_TRANSPORT_FAILED')
        return envelope['body']
    except subprocess.TimeoutExpired:
        raise TimeoutError('PUBLIC_WINDOWS_TRANSPORT_TIMEOUT') from None
    except (ValueError, OSError):
        raise RuntimeError('PUBLIC_WINDOWS_TRANSPORT_FAILED') from None


def _valid_symbol(symbol):
    return isinstance(symbol, str) and len(symbol) <= 24 and symbol.endswith('USDT') and symbol[:-4].isalnum()


def public_cross_market_payload(symbol, *, now, timeout_seconds, network_transport, transport):
    if not _valid_symbol(symbol):
        raise ValueError('PUBLIC_SYMBOL_INVALID')
    base = (
        ('binance_oi', 'https://fapi.binance.com/futures/data/openInterestHist',
            {'symbol': symbol, 'period': '15m', 'limit': '6'}),
        ('binance_premium', 'https://fapi.binance.com/fapi/v1/premiumIndex', {'symbol': symbol}),
        ('binance_funding', 'https://fapi.binance.com/fapi/v1/fundingRate', {'symbol': symbol, 'limit': '2'}),
        ('bybit_oi', 'https://api.bybit.com/v5/market/open-interest',
            {'category': 'linear', 'symbol': symbol, 'intervalTime': '15min', 'limit': '6'}),
        ('bybit_ticker', 'https://api.bybit.com/v5/market/tickers', {'category': 'linear', 'symbol': symbol}),
        ('bybit_instruments', 'https://api.bybit.com/v5/market/instruments-info', {'category': 'linear', 'symbol': symbol, 'limit': '1'}),
        ('bybit_funding', 'https://api.bybit.com/v5/market/funding/history', {'category': 'linear', 'symbol': symbol, 'limit': '2'}),
    )
    deadline_seconds = timeout_seconds
    requests = [{'key': key, 'url': url, 'params': params} for key, url, params in base]
    if network_transport == 'windows_system_proxy':
        first = windows_public_get_many(requests, timeout_seconds=deadline_seconds)
    else:
        first = {}
        for key, url, params in base:
            try:
                if getattr(transport, '__name__', '') == 'public_json_transport':
                    first[key] = transport(url, params, {}, timeout_seconds=deadline_seconds)
                else:
                    first[key] = transport(url, params, {})
            except Exception:
                first[key] = {'__transport_error': 'PUBLIC_GET_FAILED'}
    open_interest = first.get('bybit_oi') or {}
    rows = ((open_interest.get('result') or {}).get('list') or []) if isinstance(open_interest, dict) else []
    timestamps = []
    for row in rows:
        if isinstance(row, dict):
            try:
                value = int(row.get('timestamp'))
                if value > 0:
                    timestamps.append(value)
            except (TypeError, ValueError, OverflowError):
                pass
    mark = {'__transport_error': 'PUBLIC_MARK_PRICE_HISTORY_UNAVAILABLE'}
    timestamps = sorted(set(timestamps))
    now_ms = int(now.timestamp() * 1000)
    period_ms = 15 * 60 * 1000
    last_closed_ms = now_ms // period_ms * period_ms - period_ms
    closed_samples = [timestamp for timestamp in timestamps if timestamp <= last_closed_ms]
    if len(closed_samples) >= 2:
        start = closed_samples[-2] - 60_000
        end = closed_samples[-1]
        url = 'https://api.bybit.com/v5/market/mark-price-kline'
        params = {'category': 'linear', 'symbol': symbol, 'interval': '1', 'start': str(start), 'end': str(end)}
        try:
            if network_transport == 'windows_system_proxy':
                mark_result = windows_public_get_many([{'key': 'mark', 'url': url, 'params': params}], timeout_seconds=deadline_seconds)
                mark = mark_result.get('mark') or mark
            elif getattr(transport, '__name__', '') == 'public_json_transport':
                mark = transport(url, params, {}, timeout_seconds=deadline_seconds)
            else:
                mark = transport(url, params, {})
        except Exception:
            pass
    binance = {
        'open_interest_history': first.get('binance_oi'),
        'premium_index': first.get('binance_premium'),
        'funding_history': first.get('binance_funding'),
    }
    bybit = {
        'open_interest': first.get('bybit_oi'),
        'mark_price_symbol': symbol,
        'mark_price_klines': mark,
        'tickers': first.get('bybit_ticker'),
        'instruments': first.get('bybit_instruments'),
        'funding_history': first.get('bybit_funding'),
    }
    return {'binance': binance, 'bybit': bybit}
