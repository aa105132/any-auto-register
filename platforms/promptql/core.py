"""PromptQL 平台核心：OTP 注册 + Hasura DDN thread LLM（2api 包装）。

promptql 是 Hasura DDN agent 平台（"Team AI with a wiki"），认证 auth.pro.ql.app（Hasura OIDC）。
注册两步 OTP：
  1. POST auth.pro.ql.app/otp/send{email,captcha_token} → {message,nonce}
  2. POST auth.pro.ql.app/otp/verify{email,otp,nonce} → 200 set session cookie (hasura-lux)
Turnstile sitekey: 0x4AAAAAADsy_TOiX96NjTFT（page url prompt.ql.app/login）。

chat 架构（_wf_promptql_full_result.json / _wf_promptql_e2e_chat 抓包确认）：
  - console: https://data.pro.ql.app/v1/graphql（Cookie: hasura-lux=<token> auth）
      ddnCreatePromptQLProject(name,title,is_joinable) → {id, project{id, endpoint, ...}}
      project_id = project.id, build_fqdn = project.endpoint (https://data.prompt.ql.app/p-<slug>)
  - 项目级 JWT: GET https://auth.pro.ql.app/ddn/promptql/token（Cookie auth）→ {token: luxJWT}
  - chat 端点: https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql + wss 同端点
      EnrichToken(luxJWT, projectId)（Cookie auth）→ {userDirectoryJWT}
      后续 HTTP/WS 用 Authorization: Bearer <userDirectoryJWT>
      getRoomsByProjectId(projectID) → rooms[{room_id, name}]（取 general）
      CreateEmptyThread(projectId,title,visibility,roomId) → {thread_id}
      send_thread_message(message,threadId,timezone,buildFqdn?) → {thread_event_id}（自适应 mutation 名）
      WS subscription getThreadEventsStream(thread_id, after_event_id) → thread_events_stream[].event_data
        AgentMessage.update.content.interaction_update.response_generation.text（LLM 回复流）
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import requests

SITE_URL = "https://promptql.io"
APP_URL = "https://prompt.ql.app"
AUTH_BASE = "https://auth.pro.ql.app"
TURNSTILE_SITEKEY = "0x4AAAAAADsy_TOiX96NjTFT"
OAUTH_CLIENT_ID = "2e126f16-0d98-4890-9431-f4065f133e73"

OTP_SEND_API = f"{AUTH_BASE}/otp/send"
OTP_VERIFY_API = f"{AUTH_BASE}/otp/verify"
LUX_TOKEN_API = f"{AUTH_BASE}/ddn/promptql/token"

# GraphQL 端点
CONSOLE_GRAPHQL = "https://data.pro.ql.app/v1/graphql"
HGE_GRAPHQL = "https://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"
HGE_WS = "wss://data.prompt.ql.app/promptql/playground-v2-hge/v1/graphql"

# 模型库（PromptQL agent 单一模型，per_thread_model_selection 可选；llmType 待抓具体值）
PROMPTQL_MODELS = [
    "promptql-agent",
    "gpt-5", "claude-sonnet-4.6", "claude-opus-4.7",
]
DEFAULT_MODEL = "promptql-agent"
FREE_MODELS = list(PROMPTQL_MODELS)

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [promptql] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [promptql] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _gql_post(session: requests.Session, url: str, body: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: float = 40.0) -> dict[str, Any]:
    """发 GraphQL 请求，返回 {status, json, raw}。"""
    h = {"content-type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    resp = session.post(url, json=body, headers=h, timeout=timeout)
    out: dict[str, Any] = {"status": resp.status_code, "raw": resp.text[:3000]}
    try:
        out["json"] = resp.json()
    except Exception:
        out["json"] = None
    return out


class PromptQLClient:
    """PromptQL HTTP/WS 客户端：OTP 注册（Turnstile）+ session + thread LLM。

    proxy 走任务代理。access_token = hasura-lux session cookie。
    chat 流程：create_project → luxJWT → EnrichToken → userDirectoryJWT →
              getRooms → CreateEmptyThread → send_message → WS getThreadEventsStream 收回复。
    """

    def __init__(self, *, proxy: str | None = None, log_fn=print) -> None:
        self.proxy = proxy
        self.log = log_fn or log
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": APP_URL,
                "Referer": f"{APP_URL}/",
                "User-Agent": CHROME_UA,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _cookie_headers(self, access_token: str) -> dict[str, str]:
        return {"Cookie": f"hasura-lux={access_token}"}

    def otp_send(self, *, email: str, captcha_token: str = "") -> dict[str, Any]:
        """Step1: POST /otp/send{email,captcha_token} → {message,nonce}。"""
        body = {"email": str(email or "").strip().lower()}
        if captcha_token:
            body["captcha_token"] = captcha_token
        resp = self.session.post(OTP_SEND_API, json=body, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        return data

    def otp_verify(self, *, email: str, otp: str, nonce: str) -> dict[str, Any]:
        """Step2: POST /otp/verify{email,otp,nonce} → 200 set session cookie。"""
        body = {"email": str(email or "").strip().lower(), "otp": str(otp or "").strip(), "nonce": str(nonce or "")}
        resp = self.session.post(OTP_VERIFY_API, json=body, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        data["cookies"] = {c.name: c.value for c in self.session.cookies}
        return data

    # ===== chat 链路 =====

    def create_project(self, *, access_token: str, name: str = "api-project", title: str = "api-project") -> dict[str, Any]:
        """console ddnCreatePromptQLProject（cookie auth）→ {project_id, project_name, build_fqdn, raw}。

        dataplane_id 可选（省略时 Hasura 自动配 cloud dataplane）。返回 project.endpoint 作占位 build_fqdn，
        真正 build_fqdn 需随后 get_build_fqdn() 取 environments[0].build.fqdn（p-<slug>-<hash>.data.prompt.ql.app）。
        """
        mut = (
            "mutation CP($n:String!,$t:String!,$j:Boolean){"
            "ddnCreatePromptQLProject(name:$n,title:$t,is_joinable:$j){"
            "id name project{id name title endpoint ddn_id}"
            "}}"
        )
        r = _gql_post(self.session, CONSOLE_GRAPHQL,
                      {"query": mut, "operationName": "CP", "variables": {"n": name, "t": title, "j": False}},
                      headers=self._cookie_headers(access_token))
        out: dict[str, Any] = {"status": r["status"], "raw": r["raw"]}
        try:
            node = r["json"]["data"]["ddnCreatePromptQLProject"]["project"]
            out["project_id"] = str(node.get("id") or "")
            out["project_name"] = str(node.get("name") or "")
            out["build_fqdn"] = str(node.get("endpoint") or "")  # 占位，真正值见 get_build_fqdn
            out["ddn_id"] = str(node.get("ddn_id") or "")
        except Exception:
            out["project_id"] = ""
            out["project_name"] = ""
            out["build_fqdn"] = ""
        return out

    def get_build_fqdn(self, *, access_token: str, project_name: str, retries: int = 12, delay: float = 5.0) -> str:
        """取真正 build fqdn（p-<slug>-<hash>.data.prompt.ql.app）。

        抓包确认：getProjectContext($projectName).ddn_projects[0].environments[0].build.fqdn。
        新建 project 后 build 需 provisioning，故轮询 retries 次。
        """
        q = ("query gPC($pn:String!){ddn_projects(where:{name:{_eq:$pn}}){"
             "environments(order_by:{created_at:desc},limit:1){build{fqdn id hibernated}}}}")
        for attempt in range(retries):
            r = _gql_post(self.session, CONSOLE_GRAPHQL,
                          {"query": q, "operationName": "gPC", "variables": {"pn": project_name}},
                          headers=self._cookie_headers(access_token))
            try:
                projs = (r["json"].get("data") or {}).get("ddn_projects") or []
                if projs:
                    envs = projs[0].get("environments") or []
                    if envs:
                        b = envs[0].get("build") or {}
                        fqdn = str(b.get("fqdn") or "")
                        if fqdn and not b.get("hibernated"):
                            return fqdn
            except Exception:
                pass
            self.log(f"get_build_fqdn attempt{attempt+1} 无 build（provisioning），{delay}s 后重试")
            if attempt < retries - 1:
                time.sleep(delay)
        return ""

    def get_lux_jwt(self, *, access_token: str, project_id: str = "") -> str:
        """POST auth.pro.ql.app/ddn/promptql/token（Cookie auth，空 body）→ 项目级 luxJWT。"""
        try:
            resp = self.session.post(LUX_TOKEN_API, json={},
                                     headers={**self._cookie_headers(access_token), "Accept": "application/json"},
                                     timeout=30)
            if resp.status_code == 200:
                return str((resp.json() or {}).get("token") or "")
        except Exception as exc:
            self.log(f"get_lux_jwt err {exc!r}")
        return ""

    def enrich_token(self, *, access_token: str, lux_jwt: str, project_id: str) -> str:
        """EnrichToken(luxJWT, projectId)（Cookie auth）→ userDirectoryJWT（hge 端点 Bearer token）。"""
        mut = ("mutation EnrichToken($luxJWT:String!,$projectId:uuid!){"
               "enrich_token(luxJWT:$luxJWT,projectId:$projectId){userDirectoryJWT}}")
        r = _gql_post(self.session, HGE_GRAPHQL,
                      {"query": mut, "operationName": "EnrichToken", "variables": {"luxJWT": lux_jwt, "projectId": project_id}},
                      headers=self._cookie_headers(access_token))
        try:
            return str(r["json"]["data"]["enrich_token"]["userDirectoryJWT"] or "")
        except Exception:
            self.log(f"enrich_token err status={r['status']} raw={r['raw'][:200]}")
            return ""

    def _bearer(self, user_directory_jwt: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {user_directory_jwt}"}

    def get_rooms(self, *, user_directory_jwt: str, project_id: str) -> list[dict[str, Any]]:
        """getRoomsByProjectId → rooms 列表（取 general 或首个）。"""
        q = ("query GR($pid:uuid!){rooms(where:{project_id:{_eq:$pid},deleted_at:{_is_null:true}})"
             "{room_id name visibility}}")
        r = _gql_post(self.session, HGE_GRAPHQL,
                      {"query": q, "operationName": "GR", "variables": {"pid": project_id}},
                      headers=self._bearer(user_directory_jwt))
        try:
            return list(r["json"]["data"]["rooms"] or [])
        except Exception:
            return []

    def create_thread(self, *, user_directory_jwt: str, project_id: str, room_id: str = "", title: str = "api-thread") -> str:
        """CreateEmptyThread(projectId,title,visibility,roomId) → thread_id。

        重试 5 次（build provisioning 偶发 internal error，等 15s 再试）。
        """
        mut = ("mutation CET($projectId:String!,$title:String,$visibility:String,$roomId:String){"
               "create_empty_thread(projectId:$projectId,title:$title,visibility:$visibility,roomId:$roomId)"
               "{thread_id title}}")
        variants = [
            {"projectId": project_id, "title": title, "visibility": None, "roomId": room_id},
            {"projectId": project_id, "title": title, "visibility": None, "roomId": ""},
            {"projectId": project_id, "title": title, "visibility": "public", "roomId": room_id},
        ]
        for attempt in range(5):
            v = variants[attempt % len(variants)]
            r = _gql_post(self.session, HGE_GRAPHQL,
                          {"query": mut, "operationName": "CET", "variables": v},
                          headers=self._bearer(user_directory_jwt))
            try:
                tj = r["json"]
                if tj and tj.get("data") and not tj.get("errors"):
                    return str(tj["data"]["create_empty_thread"]["thread_id"] or "")
            except Exception:
                pass
            self.log(f"create_thread attempt{attempt+1} err status={r['status']} raw={r['raw'][:160]}")
            if attempt < 4:
                time.sleep(15)
        return ""

    def send_message(self, *, user_directory_jwt: str, thread_id: str, message: str,
                     timezone: str = "UTC", build_fqdn: str = "") -> dict[str, Any]:
        """发用户消息 → {thread_event_id, mutation, raw, ok}。

        用 send_thread_message（抓包确认签名：agentResponseConfig/buildFqdn/executionMode/message!/threadId!/timezone!/uploads，
        返回 SendThreadEventOutput{thread_event_id,message_id}），agentResponseConfig=force_respond 强制 agent 回复。
        兜底 send_system_message（不保证触发 agent，但已确认存在）。
        """
        arg_str = "message:$message,threadId:$threadId,timezone:$timezone,agentResponseConfig:$agentResponseConfig,buildFqdn:$buildFqdn,executionMode:$executionMode,uploads:$uploads"
        vdefs = ("$message:String!,$threadId:String!,$timezone:String!,$agentResponseConfig:String,"
                 "$buildFqdn:String,$executionMode:String,$uploads:[UserUploadInput!]")
        mut = ("mutation SM(" + vdefs + "){send_thread_message(" + arg_str + "){thread_event_id message_id}}")
        vars_: dict[str, Any] = {"message": message, "threadId": thread_id, "timezone": timezone,
                                 "agentResponseConfig": "force_respond", "buildFqdn": build_fqdn or "",
                                 "executionMode": "pyodide", "uploads": []}
        r = _gql_post(self.session, HGE_GRAPHQL,
                      {"query": mut, "operationName": "SM", "variables": vars_},
                      headers=self._bearer(user_directory_jwt))
        if r["status"] == 200 and r["json"] and r["json"].get("data") and not r["json"].get("errors"):
            node = r["json"]["data"].get("send_thread_message") or {}
            eid = int(node.get("thread_event_id") or 0) or 0
            return {"thread_event_id": eid, "mutation": "send_thread_message", "raw": r["raw"][:800], "ok": bool(eid or node)}
        # 兜底 send_system_message
        self.log(f"send_thread_message 失败，兜底 send_system_message: {r['raw'][:200]}")
        ssm_mut = ("mutation SSM($threadId:String!,$message:String!,$timezone:String!){"
               "send_system_message(threadId:$threadId,message:$message,timezone:$timezone)"
               "{thread_event_id message_id}}")
        r2 = _gql_post(self.session, HGE_GRAPHQL,
                      {"query": ssm_mut, "operationName": "SSM",
                       "variables": {"threadId": thread_id, "message": message, "timezone": timezone}},
                      headers=self._bearer(user_directory_jwt))
        try:
            node = r2["json"]["data"]["send_system_message"]
            return {"thread_event_id": int(node.get("thread_event_id") or 0), "mutation": "send_system_message",
                    "raw": r2["raw"][:800], "ok": True}
        except Exception:
            return {"thread_event_id": 0, "mutation": "", "raw": r2["raw"][:800], "ok": False}

    def collect_reply(self, *, user_directory_jwt: str, thread_id: str, after_event_id: int = 0,
                      timeout: float = 150.0, poll_interval: float = 3.0) -> dict[str, Any]:
        """HTTP 轮询 getThreadEvents 收 AgentMessage 回复（避开 WS 网关 400）。

        抓包确认 HTTP query: getThreadEvents($thread_id,$after_event_id,$limit){thread_events(where...){thread_event_id event_data ...}}
        每 poll_interval 秒查一次新事件，提取回复文本，直到完成标记或超时。
        流式回复聚合：response_generation 可能分多帧 delta 到达，按 event_id 顺序拼接；
        若某帧含完整长文（>2x 其余拼接长度）则取该帧（终态全量）。
        返回 {text, frames(events), is_error, completed, last_event_id}。
        """
        q = ("query getThreadEvents($thread_id:uuid,$after_event_id:bigint!,$limit:Int=null){"
             "thread_events(where:{thread_id:{_eq:$thread_id},thread_event_id:{_gt:$after_event_id}},"
             "order_by:{thread_event_id:asc},limit:$limit)"
             "{thread_event_id event_data created_at}}")
        bearer = self._bearer(user_directory_jwt)
        events: list[dict[str, Any]] = []
        frags: list[str] = []          # response_generation 文本片段（按 event 顺序）
        longest = ""
        cursor = int(after_event_id or 0)
        deadline = time.time() + timeout
        completed = False
        last_new_at = time.time()
        while time.time() < deadline:
            r = _gql_post(self.session, HGE_GRAPHQL,
                          {"query": q, "operationName": "getThreadEvents",
                           "variables": {"thread_id": thread_id, "after_event_id": str(cursor), "limit": None}},
                          headers=bearer, timeout=30.0)
            try:
                rows = (r["json"].get("data") or {}).get("thread_events") or []
            except Exception:
                rows = []
            if rows:
                last_new_at = time.time()
                for ev in rows:
                    eid = int(ev.get("thread_event_id") or 0) or 0
                    if eid > cursor:
                        cursor = eid
                    ed = ev.get("event_data") or {}
                    events.append({"thread_event_id": eid, "event_data": ed})
                    txt = _extract_reply_text(ed)
                    if txt:
                        frags.append(txt)
                        if len(txt) > len(longest):
                            longest = txt
                    ej = json.dumps(ed)
                    # 完成信号（抓包确认）：interaction_finished / main_agent.completed / actions_parsed.terminal.done
                    if any(k in ej for k in ("interaction_finished", '"completed": {}',
                                              '"completed":{}', '"terminal"', '"done"')):
                        completed = True
                if completed and longest:
                    break
                # 已收够多帧且静默 8s 无新事件 → 视为完成
                if longest and (time.time() - last_new_at) > 8.0:
                    completed = True
                    break
            elif events and longest and (time.time() - last_new_at) > 12.0:
                # 有回复但已 12s 无新事件，收尾
                completed = True
                break
            time.sleep(poll_interval)
        # 文本聚合：若最长帧远超其余拼接（终态全量），取最长；否则按序拼接 delta
        if longest and len(longest) > 40 and len(longest) * 2 > sum(len(f) for f in frags):
            text = longest
        elif frags:
            # 去重：若后续帧包含前面所有内容（全量替换），取最长；否则拼接
            text = _merge_fragments(frags)
        else:
            text = ""
        return {"text": text.strip(), "frames": events[:80], "is_error": False,
                "completed": completed, "last_event_id": cursor}


    def chat(
        self,
        *,
        access_token: str,
        messages: list[dict[str, str]],
        project_id: str = "",
        build_fqdn: str = "",
        model: str = DEFAULT_MODEL,
        stream: bool = False,
        timeout: float = 90.0,
    ) -> dict[str, Any]:
        """端到端 chat：取最后 user 消息发到 PromptQL thread，收 agent 回复。

        access_token = hasura-lux session cookie。若缺 project_id 则新建 project。
        返回 {text, stop_reason, is_error, project_id, build_fqdn, thread_id, frames}。
        """
        # 取最后 user 消息作 prompt（system 拼前）
        sys_parts: list[str] = []
        prompt = ""
        for msg in messages or []:
            role = str(msg.get("role") or "").lower()
            content = str(msg.get("content") or "")
            if role == "system" and content:
                sys_parts.append(content)
            elif role == "user":
                prompt = content
        if sys_parts:
            prompt = "\n\n".join(sys_parts) + "\n\n" + prompt
        if not prompt:
            prompt = "Hello"

        # 1. 建 project（若缺）
        project_name = ""
        if not project_id:
            pj = self.create_project(access_token=access_token)
            project_id = pj.get("project_id") or ""
            project_name = pj.get("project_name") or ""
            build_fqdn = pj.get("build_fqdn") or build_fqdn  # 占位 endpoint
            if not project_id:
                return {"text": "", "stop_reason": "create_project_failed", "is_error": True,
                        "project_id": "", "build_fqdn": build_fqdn, "thread_id": "", "frames": [],
                        "error": pj.get("raw", "")[:300]}

        # 1b. 取真正 build fqdn（p-<slug>-<hash>.data.prompt.ql.app，agent catalog 必需）
        # build_fqdn 占位是 https://data.prompt.ql.app/p-<slug>，真正值不含 https://
        if not build_fqdn or build_fqdn.startswith("http"):
            if not project_name:
                # project_name = p-<uuid前13字符>（PromptQL 命名规律：p-<first-two-groups>）
                project_name = "p-" + project_id[:13] if project_id else ""
            if project_name:
                real_bf = self.get_build_fqdn(access_token=access_token, project_name=project_name)
                if real_bf:
                    build_fqdn = real_bf
                else:
                    self.log(f"未取到 build_fqdn（project={project_name}），用占位 {build_fqdn}")

        # 2. luxJWT
        lux = self.get_lux_jwt(access_token=access_token, project_id=project_id)
        if not lux:
            return {"text": "", "stop_reason": "no_lux_jwt", "is_error": True, "project_id": project_id,
                    "build_fqdn": build_fqdn, "thread_id": "", "frames": []}

        # 3. EnrichToken → userDirectoryJWT
        ujwt = self.enrich_token(access_token=access_token, lux_jwt=lux, project_id=project_id)
        if not ujwt:
            return {"text": "", "stop_reason": "enrich_failed", "is_error": True, "project_id": project_id,
                    "build_fqdn": build_fqdn, "thread_id": "", "frames": []}

        # 4. rooms → room_id
        rooms = self.get_rooms(user_directory_jwt=ujwt, project_id=project_id)
        room_id = ""
        for r in rooms:
            if r.get("name") == "general":
                room_id = str(r.get("room_id") or "")
                break
        if not room_id and rooms:
            room_id = str(rooms[0].get("room_id") or "")

        # 5. CreateEmptyThread
        thread_id = self.create_thread(user_directory_jwt=ujwt, project_id=project_id, room_id=room_id)
        if not thread_id:
            return {"text": "", "stop_reason": "create_thread_failed", "is_error": True, "project_id": project_id,
                    "build_fqdn": build_fqdn, "thread_id": "", "frames": []}

        # 6. send message
        sm = self.send_message(user_directory_jwt=ujwt, thread_id=thread_id, message=prompt,
                               build_fqdn=build_fqdn)
        after_event_id = int(sm.get("thread_event_id") or 0)
        if not sm.get("ok"):
            return {"text": "", "stop_reason": "send_failed", "is_error": True, "project_id": project_id,
                    "build_fqdn": build_fqdn, "thread_id": thread_id, "frames": [], "error": sm.get("raw", "")[:300]}

        # 7. HTTP 轮询 getThreadEvents 收回复（WS 网关对 websocket-client 返 400，浏览器才走 WS）
        cr = self.collect_reply(user_directory_jwt=ujwt, thread_id=thread_id,
                                after_event_id=after_event_id, timeout=timeout, poll_interval=3.0)
        out: dict[str, Any] = {"text": cr.get("text") or "", "stop_reason": "end_turn" if cr.get("text") else "no_reply",
                "is_error": cr.get("is_error", False), "project_id": project_id, "build_fqdn": build_fqdn,
                "thread_id": thread_id, "frames": cr.get("frames", []), "send_mutation": sm.get("mutation", ""),
                "completed": cr.get("completed", False), "last_event_id": cr.get("last_event_id", 0)}
        return out


def _extract_reply_text(event_data: Any) -> str:
    """从 thread event_data 提取 LLM 回复文本片段（精确路径，避免 wiki_selection RAG 片段误判）。

    回复流路径（抓包确认 AgentMessage.update.content.interaction_update）：
      - interaction_update.response_generation.{text|content|delta|message}
      - interaction_update.main_agent.{text|content|delta}
      - AgentMessage.update.ResponseGenerationUpdate.{text|content|delta}
      - interaction_update.response_generation.chunks[].text（流式分片）
    wiki_selection.search_hits[].snippet 是 RAG 上下文，不算回复，跳过。
    """
    if not event_data:
        return ""
    if isinstance(event_data, str):
        try:
            event_data = json.loads(event_data)
        except Exception:
            return ""
    if not isinstance(event_data, dict):
        return ""
    am = event_data.get("AgentMessage") or {}
    upd = am.get("update") or {}
    content = upd.get("content") or {}
    iu = content.get("interaction_update") or {}
    # 0. main_agent.llm_response.response_text（抓包确认真实路径，含 <final_response> XML 包裹）
    ma = iu.get("main_agent") or {}
    lr = ma.get("llm_response") or {}
    for k in ("response_text", "text", "content"):
        v = lr.get(k)
        if isinstance(v, str) and v and not _looks_like_wiki(v):
            return _strip_action_xml(v)
    # 1. response_generation（旧路径备用）
    rg = iu.get("response_generation") or {}
    for k in ("text", "content", "delta", "message"):
        v = rg.get(k)
        if isinstance(v, str) and v and not _looks_like_wiki(v):
            return _strip_action_xml(v)
    # response_generation.chunks[].text
    chunks = rg.get("chunks") or rg.get("deltas") or []
    if isinstance(chunks, list):
        parts = []
        for c in chunks:
            if isinstance(c, dict):
                t = c.get("text") or c.get("content") or c.get("delta")
                if isinstance(t, str) and t:
                    parts.append(t)
            elif isinstance(c, str) and c:
                parts.append(c)
        if parts:
            return _strip_action_xml("".join(parts))
    # 2. main_agent 顶层 text/content
    for k in ("text", "content", "delta", "message"):
        v = ma.get(k)
        if isinstance(v, str) and v:
            return _strip_action_xml(v)
    # 3. ResponseGenerationUpdate
    rgu = upd.get("ResponseGenerationUpdate") or content.get("ResponseGenerationUpdate") or {}
    for k in ("text", "content", "delta"):
        v = rgu.get(k)
        if isinstance(v, str) and v:
            return _strip_action_xml(v)
    # 4. final_response / completed 标记里可能带全文
    fr = iu.get("final_response") or iu.get("final_response_sent") or {}
    if isinstance(fr, dict):
        for k in ("text", "content", "message"):
            v = fr.get(k)
            if isinstance(v, str) and v:
                return _strip_action_xml(v)
    return ""


def _strip_action_xml(text: str) -> str:
    """从 PromptQL agent 的 <action><final_response>…</final_response></action> 包裹中提取纯回复。

    无包裹标签时原样返回。去掉 <thinking>/<learning_block>/<done/> 等动作标签内容。
    """
    if not text:
        return ""
    import re
    m = re.search(r"<final_response>\s*(.*?)\s*</final_response>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    if "<action>" in text or "</action>" in text:
        cleaned = re.sub(r"<action>.*?</action>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"<[^>]+/?>", "", cleaned).strip()
        return cleaned  # 纯动作标签（learning_block/done 无内容）→ 空串，不算回复
    return text.strip()


def _looks_like_wiki(text: str) -> bool:
    """启发式：含 wiki 引用标记的 RAG snippet 不算回复正文。"""
    if not text:
        return False
    return ("wiki-promptql://" in text) or ("<wiki>" in text.lower())


def _merge_fragments(frags: list[str]) -> str:
    """合并流式回复片段。

    若某后续片段完整包含前面所有片段内容（全量替换式），取最长片段；
    否则按序拼接 delta（去前缀重叠）。
    """
    if not frags:
        return ""
    longest = max(frags, key=len)
    if len(longest) > 40 and all((longest.find(f) >= 0) for f in frags if len(f) > 8 and f != longest):
        return longest
    # 按序拼接，去相邻重叠
    out = ""
    for f in frags:
        if not f:
            continue
        if not out:
            out = f
            continue
        # 去前缀重叠（上一段尾部 == 这段开头）
        overlap = 0
        max_ov = min(len(out), len(f), 60)
        for k in range(max_ov, 4, -1):
            if out.endswith(f[:k]):
                overlap = k
                break
        out += f[overlap:] if overlap else f
    return out


def account_preview(token: str) -> str:
    raw = str(token or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
