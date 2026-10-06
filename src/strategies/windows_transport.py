"""One bounded HTTPS request using the host's existing Windows system proxy."""
import json
import os
from pathlib import Path
import subprocess
from quant_phase6.ai import ProviderError,AIErrorCode

# Static program: request data and secrets arrive over stdin, never command args.
SCRIPT=r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
try {
    $packet=[Console]::In.ReadToEnd() | ConvertFrom-Json
    if (@('https://api.openai.com/v1/responses','https://xfastapi.ai/responses') -notcontains $packet.url) { throw 'ENDPOINT_BLOCKED' }
    [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
    $web=[Net.HttpWebRequest]::Create($packet.url)
    $web.Method='POST'
    $web.Proxy=[Net.WebRequest]::DefaultWebProxy
    $web.AllowAutoRedirect=$false
    $web.Timeout=[Math]::Max(1,[int]($packet.timeout_seconds*1000))
    $web.ReadWriteTimeout=$web.Timeout
    $web.UserAgent='QuantPaperV2/2.0'
    $web.Accept='application/json'
    $web.ContentType='application/json'
    $web.Headers['Authorization']=$packet.headers.Authorization
    $encoded=[Text.Encoding]::UTF8.GetBytes(($packet.body | ConvertTo-Json -Depth 80 -Compress))
    $web.ContentLength=$encoded.Length
    $stream=$web.GetRequestStream()
    $stream.Write($encoded,0,$encoded.Length)
    $stream.Close()
    $response=$web.GetResponse()
    $inputStream=$response.GetResponseStream()
    $memory=New-Object IO.MemoryStream
    $buffer=New-Object byte[] 8192
    while (($count=$inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
        if (($memory.Length+$count) -gt 1048576) { throw 'RESPONSE_TOO_LARGE' }
        $memory.Write($buffer,0,$count)
    }
    $parsed=[Text.Encoding]::UTF8.GetString($memory.ToArray()) | ConvertFrom-Json
    @{http_status=[int]$response.StatusCode;body=$parsed} | ConvertTo-Json -Depth 80 -Compress
} catch {
    $status=0
    if ($_.Exception.Response) { $status=[int]$_.Exception.Response.StatusCode }
    @{http_status=$status;error='WINDOWS_HTTP_REQUEST_FAILED'} | ConvertTo-Json -Compress
} finally {
    if ($inputStream) { $inputStream.Close() }
    if ($response) { $response.Close() }
}
"""

def powershell_executable():
    if os.name=='nt':
        path=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    else:path=Path('/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe')
    if not path.is_file():raise ProviderError(AIErrorCode.TRANSPORT)
    return str(path)

def windows_system_transport(url,body,headers,timeout):
    from .openai_provider import APPROVED_ENDPOINTS
    if url not in APPROVED_ENDPOINTS or not 0<timeout<=60:
        raise ProviderError(AIErrorCode.POLICY_BLOCK)
    payload=json.dumps({'url':url,'body':body,'headers':headers,'timeout_seconds':timeout},ensure_ascii=True,allow_nan=False)
    kwargs={'input':payload,'text':True,'encoding':'utf-8','stdout':subprocess.PIPE,
        'stderr':subprocess.DEVNULL,'timeout':timeout+2}
    if os.name=='nt':kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
    try:
        result=subprocess.run([powershell_executable(),'-NoLogo','-NoProfile','-NonInteractive','-Command',SCRIPT],**kwargs)
        if result.returncode!=0:raise ProviderError(AIErrorCode.TRANSPORT)
        envelope=json.loads(result.stdout.lstrip('\ufeff').strip())
        status=envelope.get('http_status')
        if status!=200:
            code=AIErrorCode.AUTHENTICATION if status in (401,403) else AIErrorCode.RATE_LIMIT if status==429 else AIErrorCode.TRANSPORT if status==0 else AIErrorCode.PROVIDER_REJECTED
            raise ProviderError(code)
        if not isinstance(envelope.get('body'),dict):raise ProviderError(AIErrorCode.INVALID_JSON)
        return envelope['body']
    except subprocess.TimeoutExpired:raise ProviderError(AIErrorCode.TIMEOUT) from None
    except ProviderError:raise
    except (ValueError,OSError):raise ProviderError(AIErrorCode.TRANSPORT) from None
