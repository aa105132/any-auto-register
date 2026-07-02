cd D:\Desktop\cat\any-auto-register
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Remove-Item -Path "scripts\_vercel_backend.log","scripts\_vercel_backend.err" -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","main:app","--host","127.0.0.1","--port","8899" `
  -RedirectStandardOutput "scripts\_vercel_backend.log" `
  -RedirectStandardError "scripts\_vercel_backend.err" `
  -NoNewWindow -PassThru
Write-Output $proc.Id
