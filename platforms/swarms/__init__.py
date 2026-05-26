"""Swarms Marketplace 平台插件。
纯协议注册链路（无浏览器）：
  1. Supabase GoTrue signup → 邮箱验证链接
  2. 邮箱确认链接 → token_hash + type=signup
  3. Supabase GoTrue verify → email confirmed
  4. password grant login → access_token + refresh_token
  5. GET /auth/v1/user → user info
  6. POST tRPC panel.createApiKey → API key
"""
